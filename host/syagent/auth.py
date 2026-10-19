"""Sign in with ChatGPT for open-source apps: login, the credential file, serialised refresh.

Spec: https://developers.openai.com/siwc/token-sharing-open-source
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import jwt

RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "syagent"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
LOGIN_TIMEOUT = 600
HTTP_TIMEOUT = 30
REFRESH_MARGIN = timedelta(minutes=10)
UNUSABLE_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}


@dataclass(frozen=True)
class Endpoints:
    issuer: str = "https://auth.openai.com"
    authorize: str = "https://auth.openai.com/api/accounts/authorize"
    token: str = "https://auth.openai.com/api/accounts/oauth/token"
    jwks: str = "https://auth.openai.com/.well-known/jwks.json"


OPENAI = Endpoints()


class LoginError(Exception):
    pass


class RefreshError(Exception):
    """Refresh failed for a reason that does not invalidate the session (network, 5xx, invalid_client)."""


class CredentialFile:
    """`credentials.json`, written atomically with mode 0600; `locked()` serialises across processes."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def token_record(response: dict) -> dict:
    """Token-endpoint response → the fields stored together (replaced together on refresh)."""
    record = {k: response[k] for k in ("access_token", "refresh_token", "token_type", "expires_in") if k in response}
    if "earliest_refresh_at" in response:
        record["earliest_refresh_at"] = response["earliest_refresh_at"]
    record["scopes"] = sorted(response.get("scope", "").split())
    record["saved_at"] = datetime.now(UTC).isoformat()
    return record


def expires_at(creds: dict) -> datetime:
    return datetime.fromisoformat(creds["saved_at"]) + timedelta(seconds=creds["expires_in"])


def refresh_due(creds: dict) -> datetime:
    due = expires_at(creds) - REFRESH_MARGIN
    earliest = creds.get("earliest_refresh_at")
    if isinstance(earliest, (int, float)):
        due = max(due, datetime.fromtimestamp(earliest, UTC))
    return due


def error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except ValueError:
        return None
    return error.get("code") if isinstance(error, dict) else error


class Auth:
    def __init__(self, path: Path, endpoints: Endpoints = OPENAI):
        self.file = CredentialFile(path)
        self.endpoints = endpoints

    def status(self) -> str:
        creds = self.file.read()
        return "ok" if creds.get("refresh_token") and PLAN_SCOPE in creds.get("scopes", []) else "login_required"

    def fresh_token(self) -> dict | None:
        """The access token for the container, refreshed first when due; None when login is required."""
        with self.file.locked():
            creds = self.file.read()
            if not creds.get("refresh_token") or PLAN_SCOPE not in creds.get("scopes", []):
                return None
            if datetime.now(UTC) >= refresh_due(creds):
                creds = self._refresh(creds)
                if creds is None:
                    return None
            return {"access_token": creds["access_token"], "expires_at": expires_at(creds).isoformat()}

    def _refresh(self, creds: dict) -> dict | None:
        try:
            response = httpx.post(self.endpoints.token, timeout=HTTP_TIMEOUT, data={
                "grant_type": "refresh_token", "client_id": creds["client_id"],
                "refresh_token": creds["refresh_token"], "resource": RESOURCE,
            })
        except httpx.HTTPError as e:
            raise RefreshError(f"token refresh failed: {e}") from e
        if response.status_code == 200:
            creds = {**creds, **token_record(response.json())}
            self.file.write(creds)
            return creds
        code = error_code(response)
        if code in UNUSABLE_REFRESH:
            for key in ("access_token", "refresh_token", "expires_in", "earliest_refresh_at", "saved_at"):
                creds.pop(key, None)
            self.file.write(creds)
            return None
        raise RefreshError(f"token refresh failed: HTTP {response.status_code} {code or response.text[:200]}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class CallbackServer(HTTPServer):
    """Loopback listener for the authorization redirect; bound (listening) on construction."""

    def __init__(self, port: int):
        self.query: dict | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path != CALLBACK_PATH:
                    return self.send_error(404)
                server.query = dict(urllib.parse.parse_qsl(url.query))
                body = b"<!doctype html><p>syagent: sign-in received. You can close this tab.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", port), Handler)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{CALLBACK_PATH}"

    def wait(self) -> dict:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while self.query is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LoginError("timed out waiting for the sign-in callback")
            self.timeout = remaining
            self.handle_request()
        return self.query


def open_listener(port: int, registered: bool) -> CallbackServer:
    try:
        return CallbackServer(port)
    except OSError:
        if not registered:  # initial registration must use the default callback port
            raise
        return CallbackServer(0)


def prepare_host(file: CredentialFile) -> dict:
    """Persist this host's ext_agent_host_id before its first sign-in."""
    with file.locked():
        creds = file.read()
        if "ext_agent_host_id" not in creds:
            creds["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
            file.write(creds)
        return creds


def authorize_url(endpoints: Endpoints, creds: dict, redirect_uri: str, attempt: dict) -> str:
    params = {
        "client_id": creds.get("client_id", DYNAMIC_CLIENT),
        "ext_agent_host_id": creds["ext_agent_host_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": attempt["state"],
        "nonce": attempt["nonce"],
        "code_challenge_method": "S256",
        "code_challenge": b64url(hashlib.sha256(attempt["verifier"].encode()).digest()),
    }
    if "client_id" in creds:
        if creds.get("id_token"):
            params["id_token_hint"] = creds["id_token"]
        if creds.get("email"):
            params["login_hint"] = creds["email"]
    else:
        params["agent_name_hint"] = AGENT_NAME
    return endpoints.authorize + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def issued_client(creds: dict, query: dict) -> str:
    issued = query.get("client_id")
    if "client_id" in creds:
        if issued and issued != creds["client_id"]:
            raise LoginError("callback returned a different client_id than this registration")
        return creds["client_id"]
    if not issued or issued == DYNAMIC_CLIENT:
        raise LoginError("registration incomplete: callback carried no issued client_id")
    return issued


def exchange_code(endpoints: Endpoints, client_id: str, code: str, verifier: str, redirect_uri: str) -> dict:
    response = httpx.post(endpoints.token, timeout=HTTP_TIMEOUT, data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE,
    })
    if response.status_code != 200:
        raise LoginError(f"code exchange failed: HTTP {response.status_code} {error_code(response) or response.text[:200]}")
    return response.json()


def validate_id_token(endpoints: Endpoints, id_token: str, client_id: str, nonce: str) -> dict:
    try:
        key = jwt.PyJWKClient(endpoints.jwks).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, key, algorithms=["RS256"], audience=client_id, issuer=endpoints.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise LoginError(f"invalid ID token: {e}") from e
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise LoginError("invalid ID token: nonce mismatch")
    return claims


def login(path: Path, endpoints: Endpoints = OPENAI, open_browser=webbrowser.open,
          port: int = CALLBACK_PORT) -> dict:
    """Run one authorization attempt in the system browser; returns the stored credential record."""
    file = CredentialFile(path)
    creds = prepare_host(file)
    attempt = {k: secrets.token_urlsafe(32) for k in ("state", "nonce", "verifier")}
    with open_listener(port, "client_id" in creds) as listener:
        open_browser(authorize_url(endpoints, creds, listener.redirect_uri, attempt))
        query = listener.wait()
    if not hmac.compare_digest(query.get("state", ""), attempt["state"]):
        raise LoginError("callback state does not match this sign-in attempt")
    if "error" in query:
        raise LoginError(f"authorization failed: {query['error']} {query.get('error_description', '')}".strip())
    client_id = issued_client(creds, query)
    tokens = exchange_code(endpoints, client_id, query.get("code", ""), attempt["verifier"], listener.redirect_uri)
    claims = validate_id_token(endpoints, tokens.get("id_token", ""), client_id, attempt["nonce"])
    if creds.get("subject") and creds["subject"] != claims["sub"]:
        raise LoginError("signed in as a different account than the one registered on this host")
    with file.locked():
        record = {
            **file.read(), **token_record(tokens), "issuer": claims["iss"], "subject": claims["sub"],
            "email": claims.get("email"), "client_id": client_id, "id_token": tokens["id_token"],
        }
        file.write(record)
    return record
