"""`syagent login` and `syagent serve`."""

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

from .auth import PLAN_SCOPE, Auth, LoginError, login

HOME = Path(os.environ.get("SYAGENT_HOME", "~/.syagent")).expanduser()
DOCKER_CONTEXT = os.environ.get("SYAGENT_DOCKER_CONTEXT", "colima-syagent" if sys.platform == "darwin" else "")
AGENT_PORT = int(os.environ.get("SYAGENT_AGENT_PORT", "7700"))
WEB_HOST, WEB_PORT = "127.0.0.1", 8000
REPO = Path(__file__).resolve().parents[2]


def open_browser(url: str) -> bool:
    if "id_token_hint=" not in url:  # URLs carrying the ID token stay out of terminals and logs
        print(f"Sign in with ChatGPT: {url}", flush=True)
    print("Waiting for the browser to return to http://127.0.0.1 ...", flush=True)
    return webbrowser.open(url)


def cmd_login() -> int:
    try:
        record = login(HOME / "credentials.json", open_browser=open_browser)
    except LoginError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    if PLAN_SCOPE not in record["scopes"]:
        print("Signed in, but ChatGPT plan usage was not granted; run `syagent login` again and allow it.",
              file=sys.stderr)
        return 1
    print(f"Signed in as {record.get('email') or record['subject']}.")
    return 0


def cmd_serve() -> int:
    import uvicorn

    from .app import create_app
    from .sandbox import Sandbox

    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    sandbox = Sandbox(DOCKER_CONTEXT or None, REPO, AGENT_PORT)
    sandbox.up()
    auth = Auth(HOME / "credentials.json")
    if auth.status() != "ok":
        print("Not signed in: run `syagent login`. Serving anyway.", file=sys.stderr)
    app = create_app(HOME, sandbox, auth, f"http://127.0.0.1:{AGENT_PORT}")
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="syagent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Sign in with ChatGPT")
    sub.add_parser("serve", help="Start the sandbox and serve the web app on 127.0.0.1:8000")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(cmd_login() if args.command == "login" else cmd_serve())
