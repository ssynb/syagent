"""One owner turn: persist, push attachments, relay the agent's /turn stream, collect outbox files."""

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from .sandbox import SandboxError
from .store import Store, new_id

log = logging.getLogger(__name__)
RECORDS_PATH = "/run/syagent/records.json"
CONNECT_TIMEOUT = 10


class TurnError(Exception):
    """The request cannot become a turn (HTTP 400/404)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def safe_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\n" in name or "\0" in name:
        raise TurnError(400, f"invalid file name: {name!r}")
    return name


def file_url(message_id: str, name: str) -> str:
    return f"/api/files/{message_id}/{name}"


@dataclass
class Turn:
    conversation_id: str
    kind: str
    text: str
    uploads: list[tuple[str, bytes]] = field(default_factory=list)
    target_message_id: str | None = None
    score: int | None = None
    message_id: str = field(default_factory=new_id)
    reply_id: str = field(default_factory=new_id)

    def body(self) -> dict:
        body = {"conversation_id": self.conversation_id, "message_id": self.message_id,
                "reply_id": self.reply_id, "kind": self.kind}
        if self.kind == "feedback":
            return {**body, "target_message_id": self.target_message_id, "score": self.score, "text": self.text}
        attachments = [f"/inbox/{self.message_id}/{name}" for name, _ in self.uploads]
        return {**body, "text": self.text, "attachments": attachments}


class Turns:
    def __init__(self, store: Store, sandbox, files_dir: Path, agent_url: str):
        self.store = store
        self.sandbox = sandbox
        self.files_dir = files_dir
        self.agent_url = agent_url
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.in_flight = 0
        self.tasks: set[asyncio.Task] = set()

    def validate(self, turn: Turn) -> None:
        if not self.store.conversation_exists(turn.conversation_id):
            raise TurnError(404, "no such conversation")
        for name, _ in turn.uploads:
            safe_name(name)
        if turn.kind == "message":
            return
        if turn.kind != "feedback":
            raise TurnError(400, "kind must be message or feedback")
        target = self.store.message(turn.target_message_id or "")
        if target != {"conversation_id": turn.conversation_id, "role": "agent"}:
            raise TurnError(400, "target_message_id must be an agent message of this conversation")
        if turn.score is not None and not 1 <= turn.score <= 5:
            raise TurnError(400, "score must be 1-5")
        if turn.uploads:
            raise TurnError(400, "feedback takes no files")

    def start(self, turn: Turn) -> asyncio.Queue:
        """Runs the turn in the background (it completes even if the browser goes away)."""
        self.validate(turn)
        events: asyncio.Queue = asyncio.Queue()
        events.put_nowait({"type": "ack", "owner_message_id": turn.message_id, "agent_message_id": turn.reply_id})
        task = asyncio.create_task(self._run(turn, events))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return events

    async def _run(self, turn: Turn, events: asyncio.Queue) -> None:
        async with self.locks[turn.conversation_id]:
            self.in_flight += 1
            try:
                await self._turn(turn, events)
            except Exception as e:  # a host-side failure still ends the stream for the browser
                log.exception("turn %s failed", turn.message_id)
                events.put_nowait({"type": "error", "message": f"host error: {e}"})
            finally:
                self.in_flight -= 1
                await self.export_records()
                events.put_nowait(None)

    async def _turn(self, turn: Turn, events: asyncio.Queue) -> None:
        names = [name for name, _ in turn.uploads]
        for name, data in turn.uploads:
            self._save(turn.message_id, name, data)
        self.store.add_message(turn.conversation_id, turn.message_id, "owner", turn.kind, turn.text, names,
                               "done", turn.target_message_id, turn.score)
        try:
            for name, data in turn.uploads:
                await asyncio.to_thread(self.sandbox.write, f"/inbox/{turn.message_id}/{name}", data)
        except SandboxError as e:
            text, paths, error = "", [], f"could not copy attachments into the sandbox: {e}"
        else:
            text, paths, error = await self._relay(turn, events)
        files = []
        if error is None:
            for path in paths:
                name = PurePosixPath(path).name
                try:
                    self._save(turn.reply_id, name, await asyncio.to_thread(self.sandbox.copy_out, path))
                    files.append(name)
                except SandboxError as e:
                    error = f"could not copy {name} out of the sandbox: {e}"
                    break
        if error is not None:
            text = f"{text}\n\nError: {error}" if text else f"Error: {error}"
        self.store.add_message(turn.conversation_id, turn.reply_id, "agent", turn.kind, text, files,
                               "error" if error is not None else "done")
        events.put_nowait({"type": "error", "message": error} if error is not None else {"type": "done"})

    async def _relay(self, turn: Turn, events: asyncio.Queue) -> tuple[str, list[str], str | None]:
        """Streams agent events to `events`; returns (text, announced outbox paths, error or None)."""
        text, paths = [], []
        outbox = f"/outbox/{turn.message_id}/"
        try:
            async with (
                httpx.AsyncClient(timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT)) as client,
                client.stream("POST", f"{self.agent_url}/turn", json=turn.body()) as response,
            ):
                if response.status_code != 200:
                    return "".join(text), paths, f"agent answered HTTP {response.status_code}"
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    match event.get("type"):
                        case "text":
                            text.append(event["delta"])
                            events.put_nowait({"type": "text", "delta": event["delta"]})
                        case "file":
                            path = event["path"]
                            name = PurePosixPath(path).name
                            if not path.startswith(outbox) or name in ("", "..") or path in paths:
                                log.warning("ignoring file event outside %s: %r", outbox, path)
                                continue
                            paths.append(path)
                            events.put_nowait({"type": "file", "name": name, "url": file_url(turn.reply_id, name)})
                        case "done":
                            return "".join(text), paths, None
                        case "error":
                            return "".join(text), paths, event.get("message") or "agent error"
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return "".join(text), paths, f"agent connection failed: {type(e).__name__}: {e}"
        return "".join(text), paths, "agent closed the stream before done"

    def _save(self, message_id: str, name: str, data: bytes) -> None:
        directory = self.files_dir / message_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)

    async def export_records(self) -> None:
        data = json.dumps(self.store.export(), ensure_ascii=False).encode()
        try:
            await asyncio.to_thread(self.sandbox.write, RECORDS_PATH, data)
        except Exception:
            log.exception("could not push records.json into the sandbox")
