"""Web API on 127.0.0.1:8000 plus the static chat app at /."""

import asyncio
import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import Auth
from .control import Control, ControlError
from .sandbox import SandboxError
from .store import Store
from .turns import Turn, TurnError, Turns, file_url, safe_name

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
WATCHDOG_INTERVAL = 30
TOKEN_INTERVAL = 60
mimetypes.add_type("text/javascript", ".js")  # the OS registry may map .js elsewhere
mimetypes.add_type("application/manifest+json", ".webmanifest")


class Rollback(BaseModel):
    commit: str | None = None


async def every(seconds: int, job) -> None:
    while True:
        try:
            await job()
        except Exception:
            log.exception("%s failed", job.__name__)
        await asyncio.sleep(seconds)


def create_app(home: Path, sandbox, auth: Auth, agent_url: str) -> FastAPI:
    store = Store(home / "syagent.db")
    files_dir = home / "files"
    turns = Turns(store, sandbox, files_dir, agent_url)
    control = Control(store, sandbox, auth, turns, agent_url)

    async def watchdog():
        await asyncio.to_thread(sandbox.ensure_running)  # re-fences and restarts after a VM reboot
        await control.check_health()

    @asynccontextmanager
    async def lifespan(app):
        await control.resync()
        tasks = [asyncio.create_task(every(WATCHDOG_INTERVAL, watchdog)),
                 asyncio.create_task(every(TOKEN_INTERVAL, control.push_token))]
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(TurnError)
    @app.exception_handler(ControlError)
    async def client_error(request: Request, e: TurnError | ControlError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.exception_handler(SandboxError)
    async def sandbox_error(request: Request, e: SandboxError):
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.get("/api/status")
    def status():
        return control.status()

    @app.get("/api/conversations")
    def conversations():
        return store.conversations()

    @app.post("/api/conversations")
    def create_conversation():
        return {"id": store.create_conversation()}

    @app.get("/api/conversations/{cid}/messages")
    def messages(cid: str):
        if not store.conversation_exists(cid):
            raise HTTPException(404, "no such conversation")
        return [{**m, "files": [{"name": n, "url": file_url(m["id"], n)} for n in m["files"]]}
                for m in store.messages(cid)]

    @app.post("/api/conversations/{cid}/messages")
    async def post_message(
        cid: str,
        kind: Annotated[str, Form()],
        text: Annotated[str, Form()] = "",
        files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
        target_message_id: Annotated[str | None, Form()] = None,
        score: Annotated[int | None, Form()] = None,
    ):
        uploads = [(f.filename or "", await f.read()) for f in files or []]
        events = turns.start(Turn(cid, kind, text, uploads, target_message_id, score))

        async def stream():
            while (event := await events.get()) is not None:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/files/{mid}/{name}")
    def download(mid: str, name: str):
        path = files_dir / safe_name(mid) / safe_name(name)
        if not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path, filename=name)

    @app.post("/api/agent/restart")
    async def restart():
        await control.restart()
        return {"ok": True}

    @app.get("/api/agent/commits")
    async def commits():
        return await control.commits()

    @app.post("/api/agent/rollback")
    async def rollback(body: Rollback | None = None):
        return await control.rollback(body.commit if body else None)

    @app.get("/api/agent/snapshots")
    async def snapshots():
        return await control.snapshots()

    @app.post("/api/agent/snapshots")
    async def snapshot():
        return await control.snapshot()

    @app.post("/api/agent/snapshots/{tag}/restore")
    async def restore(tag: str):
        await control.restore(tag)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True, check_dir=False), name="web")
    return app
