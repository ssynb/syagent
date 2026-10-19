"""Acceptance test for the host↔agent contract: main.py against a fake Responses API."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parents[1]

TOOL_FILE = '''
SPEC = {"type": "function", "name": "greet", "description": "Say hello.",
        "parameters": {"type": "object", "properties": {}}}

def run(args):
    return "hello"
'''


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def function_call_response(call_id, command):
    item = {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": "run",
            "arguments": json.dumps({"command": command}), "status": "completed"}
    return [
        {"type": "response.output_item.done", "output_index": 0, "item": item, "sequence_number": 1},
        {"type": "response.completed", "sequence_number": 2,
         "response": {"id": "resp_1", "object": "response", "status": "completed", "output": [item]}},
    ]


def text_response(text):
    item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}
    return [
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0,
         "content_index": 0, "delta": text, "logprobs": [], "sequence_number": 1},
        {"type": "response.output_item.done", "output_index": 0, "item": item, "sequence_number": 2},
        {"type": "response.completed", "sequence_number": 3,
         "response": {"id": "resp_2", "object": "response", "status": "completed", "output": [item]}},
    ]


class FakeOpenAI:
    """Replays scripted streams and records every /responses request body."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"slug": "hidden", "visibility": "hide"},
                                              {"slug": "gpt-x", "visibility": "list"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = sse(fake.script.pop(0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def agent(tmp_path):
    fake = FakeOpenAI([function_call_response("call_1", "echo hi"), text_response("all done"),
                       text_response("ok")])
    root, run_dir = tmp_path / "agent", tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    shutil.copy(SEED / "main.py", root / "main.py")
    (root / "prompt.md").write_text("You are a test agent.")
    (root / "config.toml").write_text(f'model = ""\nreasoning_effort = "high"\nbase_url = "{fake.url}"\n')
    (root / "tools").mkdir()
    (run_dir / "token").write_text(json.dumps({"access_token": "tok", "expires_at": "2099-01-01T00:00:00Z"}))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed",
                    "--allow-empty"], cwd=root, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    port = free_port()
    env = {**os.environ, "SYAGENT_PORT": str(port), "SYAGENT_RUN_DIR": str(run_dir),
           "SYAGENT_OUTBOX": str(tmp_path / "outbox")}
    proc = subprocess.Popen([sys.executable, str(root / "main.py")], env=env)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            if time.monotonic() > deadline or proc.poll() is not None:
                proc.kill()
                raise
            time.sleep(0.1)
    yield {"base": base, "fake": fake, "root": root, "head": head}
    proc.kill()
    proc.wait()
    fake.server.shutdown()


def turn(base, body):
    req = urllib.request.Request(base + "/turn", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        return [json.loads(line[5:]) for line in r.read().decode().splitlines() if line.startswith("data:")]


def test_health_reports_git_head(agent):
    with urllib.request.urlopen(agent["base"] + "/health") as r:
        assert json.load(r) == {"commit": agent["head"]}


def test_turn_runs_tool_streams_text_and_loads_new_tools(agent):
    events = turn(agent["base"], {"conversation_id": "c1", "message_id": "m1", "reply_id": "r1", "kind": "message",
                                  "text": "say hi", "attachments": []})

    assert events == [{"type": "text", "delta": "all done"}, {"type": "done"}]
    first, second = agent["fake"].requests[:2]
    assert first["model"] == "gpt-x"
    outputs = [i for i in second["input"] if i.get("type") == "function_call_output"]
    assert outputs and outputs[0]["call_id"] == "call_1" and "hi" in outputs[0]["output"].splitlines()

    (agent["root"] / "tools" / "greet.py").write_text(TOOL_FILE)
    events = turn(agent["base"], {"conversation_id": "c1", "message_id": "m2", "reply_id": "r2", "kind": "message",
                                  "text": "again", "attachments": []})

    assert events[-1] == {"type": "done"}
    assert "greet" in [t.get("name") for t in agent["fake"].requests[2]["tools"]]
