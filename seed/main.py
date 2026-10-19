"""syagent runtime: serves the host contract (GET /health, POST /turn) and runs the model loop."""

import importlib.util
import json
import os
import signal
import subprocess
import threading
import tomllib
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent
RUN_DIR = Path(os.environ.get("SYAGENT_RUN_DIR", "/run/syagent"))
OUTBOX = Path(os.environ.get("SYAGENT_OUTBOX", "/outbox"))
PORT = int(os.environ.get("SYAGENT_PORT", "7000"))
RUN_TIMEOUT = 300
OUTPUT_LIMIT = 20000
# /health reports the code this process actually runs, so the host's "last healthy commit" has really run.
STARTED_COMMIT = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
turns_in_flight = 0
turns_lock = threading.Lock()

RUN_SPEC = {
    "type": "function",
    "name": "run",
    "description": f"Run a command with `bash -lc` (cwd {ROOT}). Returns exit code, stdout and stderr.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": f"Seconds, default {RUN_TIMEOUT}."},
        },
        "required": ["command"],
    },
    "strict": False,
}


def truncate(text):
    if len(text) <= OUTPUT_LIMIT:
        return text
    half = OUTPUT_LIMIT // 2
    return f"{text[:half]}\n[... truncated {len(text) - OUTPUT_LIMIT} chars ...]\n{text[-half:]}"


def run_command(args):
    timeout = args.get("timeout") or RUN_TIMEOUT
    proc = subprocess.Popen(
        ["bash", "-lc", args["command"]], cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
        start_new_session=True,  # own process group, so a timeout kills background children too
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        status = f"exit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        status = f"timed out after {timeout}s, killed"
    return truncate(f"{status}\nstdout:\n{out}\nstderr:\n{err}")


def load_tools():
    """Built-in `run` plus every tools/*.py exporting SPEC and run(args) -> str."""
    tools, errors = {"run": (RUN_SPEC, run_command)}, []
    for path in sorted((ROOT / "tools").glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"tools.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            tools[module.SPEC["name"]] = (module.SPEC, module.run)
        except Exception as e:
            errors.append(f"{path.name}: {type(e).__name__}: {e}")
    return tools, errors


def call_tool(tools, call):
    if call["name"] not in tools:
        return f"unknown tool: {call['name']}"
    try:
        return str(tools[call["name"]][1](json.loads(call["arguments"] or "{}")))
    except Exception as e:
        return f"tool error: {type(e).__name__}: {e}"


def make_client(config):
    token = json.loads((RUN_DIR / "token").read_text())["access_token"]  # host rewrites it on refresh
    return OpenAI(api_key=token, base_url=config["base_url"])


def pick_model(client, config):
    if config.get("model"):
        return config["model"]
    listing = client.get("/models", cast_to=object)
    if "models" in listing:  # ChatGPT account listing: [{slug, visibility}]
        return next(m["slug"] for m in listing["models"] if m.get("visibility") == "list")
    return listing["data"][0]["id"]


def user_item(req):
    mid = req["message_id"]
    ids = f"message {mid}, your reply will be {req['reply_id']}"
    if req["kind"] == "feedback":
        score = req.get("score")
        text = (f"[owner feedback, {ids}] on your reply {req['target_message_id']}\n"
                f"Score: {'none' if score is None else f'{score}/5'}\n"
                f"Critique: {req.get('text') or '(none)'}")
    else:
        text = f"[owner message, {ids}]\n{req.get('text', '')}"
        if req.get("attachments"):
            text += "\nAttachments:\n" + "\n".join(req["attachments"])
    return {"role": "user", "content": f"{text}\nOutbox for this turn: {OUTBOX / mid}/"}


def model_step(client, model, config, history, emit):
    """One streamed model call. Appends output items to history; returns the function calls."""
    tools, errors = load_tools()
    instructions = (ROOT / "prompt.md").read_text()
    if errors:
        instructions += "\n\nTool files that failed to load:\n" + "\n".join(errors)
    stream = client.responses.create(
        model=model, instructions=instructions, input=history,
        tools=[spec for spec, _ in tools.values()], reasoning={"effort": config["reasoning_effort"]},
        store=False, stream=True, include=["reasoning.encrypted_content"],
    )
    calls = []
    for event in stream:
        if event.type == "response.output_text.delta":
            emit({"type": "text", "delta": event.delta})
        elif event.type == "response.output_item.done":
            item = event.item.model_dump(exclude_none=True)
            history.append(item)
            if item["type"] == "function_call":
                calls.append(item)
        elif event.type == "response.failed":
            raise RuntimeError(f"model failed: {event.response.error}")
        elif event.type == "error":
            raise RuntimeError(f"model error: {event.message}")
    for call in calls:
        history.append({"type": "function_call_output", "call_id": call["call_id"],
                        "output": call_tool(tools, call)})
    return calls


def converse(req, emit):
    state = ROOT / "state" / f"{req['conversation_id']}.json"
    history = json.loads(state.read_text()) if state.exists() else []
    history.append(user_item(req))
    kept = len(history)
    try:
        config = tomllib.loads((ROOT / "config.toml").read_text())
        model = None
        while True:
            client = make_client(config)
            model = model or pick_model(client, config)
            if not model_step(client, model, config, history, emit):
                break
    except Exception:
        del history[kept:]  # a failed step may leave a function_call without its output
        raise
    finally:
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps(history))
    outbox = OUTBOX / req["message_id"]
    if outbox.is_dir():
        for path in sorted(outbox.iterdir()):
            if path.is_file():
                emit({"type": "file", "path": str(path)})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            return self.send_error(404)
        body = json.dumps({"commit": STARTED_COMMIT}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global turns_in_flight
        if self.path != "/turn":
            return self.send_error(404)
        with turns_lock:
            turns_in_flight += 1
        try:
            self.turn()
        finally:
            with turns_lock:
                turns_in_flight -= 1
                flag = RUN_DIR / "restart"
                if turns_in_flight == 0 and flag.exists():
                    flag.unlink()
                    os._exit(0)  # the container supervisor starts us again

    def turn(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            converse(json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.emit)
            self.emit({"type": "done"})
        except Exception as e:
            traceback.print_exc()
            self.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})

    def emit(self, event):
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
