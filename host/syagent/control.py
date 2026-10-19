"""Host-side control of the agent: watchdog, token push, restart, rollback, snapshots."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import httpx

from .auth import Auth
from .sandbox import SNAPSHOT_REPO, Sandbox
from .store import Store
from .turns import Turns

log = logging.getLogger(__name__)
TOKEN_PATH = "/run/syagent/token"
HEALTH_TIMEOUT = 5
COMMIT_LOG_LIMIT = 50
TAG_FORMAT = "%Y%m%dT%H%M%SZ"
COMMIT_RE = re.compile(r"[0-9a-f]{4,64}")
TAG_RE = re.compile(r"\d{8}T\d{6}Z")
ROLLBACK = 'set -e; cd /agent; t=$(git rev-parse --verify --quiet "$1^{commit}") || { echo "unknown commit: $1" >&2; exit 3; }; git branch "$2" HEAD; ' \
           'git reset -q --hard "$t"; echo "$t"'


class ControlError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def timestamp() -> str:
    return datetime.now(UTC).strftime(TAG_FORMAT)


def tag_time(tag: str) -> str:
    return datetime.strptime(tag, TAG_FORMAT).replace(tzinfo=UTC).isoformat()


class Control:
    def __init__(self, store: Store, sandbox: Sandbox, auth: Auth, turns: Turns, agent_url: str):
        self.store = store
        self.sandbox = sandbox
        self.auth = auth
        self.turns = turns
        self.agent_url = agent_url
        self.online = False
        self.commit: str | None = None
        self.pushed_token: dict | None = None

    def status(self) -> dict:
        return {"auth": self.auth.status(), "agent": "online" if self.online else "offline",
                "commit": self.commit, "healthy_commit": self.store.get_meta("healthy_commit")}

    async def check_health(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                response = await client.get(f"{self.agent_url}/health")
            response.raise_for_status()
            self.commit = response.json()["commit"]
            self.online = True
        except (httpx.HTTPError, ValueError, KeyError) as e:
            if self.online:
                log.warning("agent went offline: %s", e)
            self.online = False
            return
        self.store.set_meta("healthy_commit", self.commit)

    async def push_token(self) -> None:
        token = await asyncio.to_thread(self.auth.fresh_token)
        if token is not None and token != self.pushed_token:
            await asyncio.to_thread(self.sandbox.write, TOKEN_PATH, json.dumps(token).encode())
            self.pushed_token = token

    def _require_idle(self) -> None:
        if self.turns.in_flight:
            raise ControlError(409, "a turn is in flight; retry when it is done")

    async def restart(self) -> None:
        self._require_idle()
        await asyncio.to_thread(self.sandbox.kill_agent)

    async def commits(self) -> list[dict]:
        out = await asyncio.to_thread(
            self.sandbox.exec, "git", "-C", "/agent", "log", f"-n{COMMIT_LOG_LIMIT}", "--format=%H%x1f%s%x1f%cI")
        return [dict(zip(("commit", "subject", "date"), line.split("\x1f"))) for line in out.splitlines()]

    async def rollback(self, commit: str | None) -> dict:
        self._require_idle()
        commit = commit or self.store.get_meta("healthy_commit")
        if not commit:
            raise ControlError(409, "no healthy commit recorded yet")
        if not COMMIT_RE.fullmatch(commit):
            raise ControlError(400, "commit must be a hex object id")
        branch = f"abandoned/{timestamp()}"
        head = await asyncio.to_thread(self.sandbox.exec, "sh", "-c", ROLLBACK, "sh", commit, branch)
        await asyncio.to_thread(self.sandbox.kill_agent)
        return {"commit": head.strip(), "abandoned": branch}

    async def snapshots(self) -> list[dict]:
        tags = await asyncio.to_thread(self.sandbox.snapshot_tags)
        return [{"tag": t, "created_at": tag_time(t)} for t in sorted(filter(TAG_RE.fullmatch, tags), reverse=True)]

    async def snapshot(self) -> dict:
        tag = timestamp()
        await asyncio.to_thread(self.sandbox.commit, tag)
        return {"tag": tag, "created_at": tag_time(tag)}

    async def restore(self, tag: str) -> None:
        self._require_idle()
        if not TAG_RE.fullmatch(tag):
            raise ControlError(404, "no such snapshot")
        await asyncio.to_thread(self.sandbox.recreate, f"{SNAPSHOT_REPO}:{tag}")
        await self.resync()

    async def resync(self) -> None:
        """Push host-owned state into a (re)created container."""
        self.pushed_token = None
        await self.push_token()
        await self.turns.export_records()
