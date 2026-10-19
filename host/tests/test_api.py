"""Web API ↔ agent contract seam: SSE relay, file transfer and records persistence.

The agent is a real HTTP server speaking the frozen /turn contract; docker is replaced by an
in-memory container filesystem (the only docker operations a turn needs are file transfers).
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from syagent.app import create_app
from syagent.auth import Auth


class FakeContainer:
    """Container filesystem as seen through the sandbox driver's file-transfer operations."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def copy_out(self, path: str) -> bytes:
        return self.files[path]


class FakeAgent:
    """Speaks POST /turn. `script(body)` returns the events to stream, or raises to drop the connection."""

    def __init__(self, container: FakeContainer):
        self.container = container
        self.bodies: list[dict] = []
        self.seen_files: list[set[str]] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.script = lambda body: [{"type": "done"}]
        agent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with agent.lock:
                    agent.bodies.append(body)
                    agent.seen_files.append(set(agent.container.files))
                    agent.active += 1
                    agent.max_active = max(agent.max_active, agent.active)
                try:
                    events = agent.script(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in events:
                        if event == "DROP":
                            return
                        if event == "SLOW":
                            time.sleep(0.2)
                            continue
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                finally:
                    with agent.lock:
                        agent.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def env(tmp_path):
    container = FakeContainer()
    agent = FakeAgent(container)
    app = create_app(tmp_path, container, Auth(tmp_path / "credentials.json"), agent.url)
    yield app, agent, container
    agent.server.shutdown()


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host")


def sse(response) -> list[dict]:
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def records(container) -> dict:
    return json.loads(container.files["/run/syagent/records.json"])


async def new_conversation(c) -> str:
    r = await c.post("/api/conversations")
    assert r.status_code == 200
    return r.json()["id"]


async def test_message_turn_relays_stream_and_persists_records(env):
    app, agent, container = env

    def script(body):
        container.files[f"/outbox/{body['message_id']}/result.txt"] = b"42"
        return [
            {"type": "text", "delta": "Hel"},
            {"type": "text", "delta": "lo"},
            {"type": "file", "path": f"/outbox/{body['message_id']}/result.txt"},
            {"type": "done"},
        ]

    agent.script = script
    async with client(app) as c:
        cid = await new_conversation(c)
        r = await c.post(
            f"/api/conversations/{cid}/messages",
            data={"kind": "message", "text": "add these"},
            files=[("files[]", ("photo.jpg", b"JPEG", "image/jpeg"))],
        )
        events = sse(r)
        ack = events[0]
        mid, rid = ack["owner_message_id"], ack["agent_message_id"]
        file_url = f"/api/files/{rid}/result.txt"
        assert events == [
            {"type": "ack", "owner_message_id": mid, "agent_message_id": rid},
            {"type": "text", "delta": "Hel"},
            {"type": "text", "delta": "lo"},
            {"type": "file", "name": "result.txt", "url": file_url},
            {"type": "done"},
        ]

        assert agent.bodies == [{
            "conversation_id": cid, "message_id": mid, "reply_id": rid, "kind": "message",
            "text": "add these", "attachments": [f"/inbox/{mid}/photo.jpg"],
        }]
        assert f"/inbox/{mid}/photo.jpg" in agent.seen_files[0], "attachment must be in the container before the turn"
        assert container.files[f"/inbox/{mid}/photo.jpg"] == b"JPEG"

        assert (await c.get(file_url)).content == b"42"
        assert (await c.get(f"/api/files/{mid}/photo.jpg")).content == b"JPEG"

        messages = (await c.get(f"/api/conversations/{cid}/messages")).json()
        assert [(m["id"], m["role"], m["kind"], m["text"], m["files"], m["status"]) for m in messages] == [
            (mid, "owner", "message", "add these", [{"name": "photo.jpg", "url": f"/api/files/{mid}/photo.jpg"}], "done"),
            (rid, "agent", "message", "Hello", [{"name": "result.txt", "url": file_url}], "done"),
        ]

        conversations = (await c.get("/api/conversations")).json()
        assert [(x["id"], x["title"]) for x in conversations] == [(cid, "add these")]

    exported = records(container)
    assert [conv["id"] for conv in exported["conversations"]] == [cid]
    owner, reply = exported["conversations"][0]["messages"]
    assert {k: owner[k] for k in owner if k != "created_at"} == {
        "id": mid, "role": "owner", "kind": "message", "text": "add these", "files": ["photo.jpg"],
        "target_message_id": None, "score": None, "status": "done",
    }
    assert (reply["id"], reply["text"], reply["files"], reply["status"]) == (rid, "Hello", ["result.txt"], "done")


async def test_feedback_turn_carries_target_and_score(env):
    app, agent, container = env
    async with client(app) as c:
        cid = await new_conversation(c)
        first = sse(await c.post(f"/api/conversations/{cid}/messages", data={"kind": "message", "text": "hi"}))[0]
        target = first["agent_message_id"]

        ack = sse(await c.post(
            f"/api/conversations/{cid}/messages",
            data={"kind": "feedback", "text": "too long", "target_message_id": target, "score": "2"},
        ))[0]
        sse(await c.post(
            f"/api/conversations/{cid}/messages",
            data={"kind": "feedback", "text": "again", "target_message_id": target},
        ))[0]

    assert agent.bodies[1] == {
        "conversation_id": cid, "message_id": ack["owner_message_id"], "reply_id": ack["agent_message_id"],
        "kind": "feedback", "target_message_id": target, "score": 2, "text": "too long",
    }
    assert agent.bodies[2]["score"] is None
    exported = records(container)["conversations"][0]["messages"]
    feedback = exported[2]
    assert (feedback["kind"], feedback["target_message_id"], feedback["score"]) == ("feedback", target, 2)
    # A reply carries the latest non-null score given to it; the later unscored critique keeps it.
    assert exported[1]["score"] == 2
    async with client(app) as c:
        listed = (await c.get(f"/api/conversations/{cid}/messages")).json()
    assert listed[1]["score"] == 2


@pytest.mark.parametrize("script, partial", [
    ([{"type": "text", "delta": "par"}, {"type": "error", "message": "model failed"}], "par"),
    ([{"type": "text", "delta": "par"}, "DROP"], "par"),
    (["DROP"], ""),
])
async def test_failed_turn_is_recorded_as_error(env, script, partial):
    app, agent, container = env
    agent.script = lambda body: script
    async with client(app) as c:
        cid = await new_conversation(c)
        events = sse(await c.post(f"/api/conversations/{cid}/messages", data={"kind": "message", "text": "hi"}))
        assert events[-1]["type"] == "error"
        messages = (await c.get(f"/api/conversations/{cid}/messages")).json()

    reply = messages[1]
    assert reply["status"] == "error"
    assert reply["text"].startswith(partial)
    assert records(container)["conversations"][0]["messages"][1]["status"] == "error"


async def test_turns_of_one_conversation_never_overlap(env):
    app, agent, _ = env
    agent.script = lambda body: ["SLOW", {"type": "done"}]
    async with client(app) as c:
        cid = await new_conversation(c)
        results = await asyncio.gather(*[
            c.post(f"/api/conversations/{cid}/messages", data={"kind": "message", "text": f"m{i}"})
            for i in range(3)
        ])
    assert [sse(r)[-1]["type"] for r in results] == ["done"] * 3
    assert agent.max_active == 1
