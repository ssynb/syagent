"""Sign in with ChatGPT against a fake OpenAI auth server: login, rotating refresh, refresh errors."""

import base64
import hashlib
import json
import stat
import threading
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from syagent.auth import Auth, Endpoints, LoginError, login

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER = "https://auth.openai.com"
CLIENT = "oaiapp_test"
SCOPE = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"


class FakeAuthServer:
    """Token endpoint with rotating refresh tokens (reuse is rejected) plus a JWKS endpoint."""

    def __init__(self):
        self.requests: list[dict] = []
        self.valid_refresh = {"rt-1"}
        self.issued = 1
        self.nonce_override = None
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
                self.reply(200, {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                server.requests.append(form)
                status, body = server.token(form)
                self.reply(status, body)

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.endpoints = Endpoints(
            issuer=ISSUER, authorize=f"{base}/authorize", token=f"{base}/token", jwks=f"{base}/jwks",
        )

    def token(self, form):
        if form["grant_type"] == "refresh_token":
            time.sleep(0.2)  # widen the window in which a second refresher could race
            with self.lock:
                if form["refresh_token"] not in self.valid_refresh:
                    return 400, {"error": "refresh_token_reused"}
                self.valid_refresh = set()
                self.issued += 1
                self.valid_refresh.add(f"rt-{self.issued}")
                return 200, self.tokens()
        return 200, {**self.tokens(), "id_token": self.id_token(form)}

    def tokens(self):
        return {"access_token": f"at-{self.issued}", "refresh_token": f"rt-{self.issued}",
                "token_type": "Bearer", "expires_in": 3600, "scope": SCOPE}

    def id_token(self, form):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": form["client_id"], "sub": "user-1", "email": "o@example.com",
                  "iat": now, "exp": now + 3600, "nonce": self.nonce_override or self.pending_nonce}
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def server():
    s = FakeAuthServer()
    yield s
    s.httpd.shutdown()


def write_credentials(path, saved_at, refresh_token="rt-1"):
    path.write_text(json.dumps({
        "client_id": CLIENT, "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000000",
        "subject": "user-1", "access_token": "at-1", "refresh_token": refresh_token, "token_type": "Bearer",
        "expires_in": 3600, "scopes": SCOPE.split(), "saved_at": saved_at.isoformat(),
    }))


def expired():
    return datetime.now(UTC) - timedelta(hours=2)


def test_concurrent_refreshes_rotate_once_and_persist_replacement(tmp_path, server):
    path = tmp_path / "credentials.json"
    write_credentials(path, expired())
    # Two instances share only the file, like two processes would.
    results = [None, None]
    threads = [
        threading.Thread(target=lambda i=i: results.__setitem__(i, Auth(path, server.endpoints).fresh_token()))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [r["access_token"] for r in results] == ["at-2", "at-2"]
    assert server.requests == [{
        "grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": "rt-1",
        "resource": "https://api.openai.com/v1",
    }]
    saved = json.loads(path.read_text())
    assert (saved["access_token"], saved["refresh_token"]) == ("at-2", "rt-2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # The next refresh must present the replacement token, not the original.
    saved["saved_at"] = expired().isoformat()
    path.write_text(json.dumps(saved))
    assert Auth(path, server.endpoints).fresh_token()["access_token"] == "at-3"
    assert server.requests[-1]["refresh_token"] == "rt-2"


def test_fresh_token_is_not_refreshed_early(tmp_path, server):
    path = tmp_path / "credentials.json"
    write_credentials(path, datetime.now(UTC))
    token = Auth(path, server.endpoints).fresh_token()
    assert token["access_token"] == "at-1"
    assert datetime.fromisoformat(token["expires_at"]) > datetime.now(UTC) + timedelta(minutes=50)
    assert server.requests == []


def test_unusable_refresh_token_requires_login_but_keeps_registration(tmp_path, server):
    path = tmp_path / "credentials.json"
    write_credentials(path, expired(), refresh_token="rt-stale")
    auth = Auth(path, server.endpoints)
    assert auth.fresh_token() is None
    assert auth.status() == "login_required"
    saved = json.loads(path.read_text())
    assert "refresh_token" not in saved and "access_token" not in saved
    assert saved["client_id"] == CLIENT
    assert saved["ext_agent_host_id"] == "urn:uuid:00000000-0000-4000-8000-000000000000"


def browser_consent(server, issued_client=CLIENT):
    """Acts as the system browser: records the authorize URL and redirects back to the loopback."""
    seen = {}

    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)
        server.pending_nonce = q["nonce"]
        callback = q["redirect_uri"] + "?" + urllib.parse.urlencode(
            {"code": "code-1", "state": q["state"], "scope": SCOPE, "client_id": issued_client})
        threading.Thread(target=lambda: httpx.get(callback), daemon=True).start()
        return True

    return seen, open_browser


def test_first_login_registers_dynamic_client_and_stores_credentials(tmp_path, server):
    path = tmp_path / "credentials.json"
    seen, open_browser = browser_consent(server)
    login(path, server.endpoints, open_browser=open_browser, port=0)

    assert seen["client_id"] == "dynamic_agent_client"
    assert seen["agent_name_hint"] == "syagent"
    assert seen["ext_agent_host_id"].startswith("urn:uuid:")
    assert seen["scope"].split() == ["openid", "profile", "email", "offline_access", "resource.invoke",
                                     "chatgpt.tokens.use.direct"]
    assert seen["resource"] == "https://api.openai.com/v1"
    assert seen["response_type"] == "code" and seen["code_challenge_method"] == "S256"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and seen["redirect_uri"].endswith("/auth/callback")

    exchange = server.requests[0]
    assert exchange["client_id"] == CLIENT and exchange["redirect_uri"] == seen["redirect_uri"]
    digest = hashlib.sha256(exchange["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == seen["code_challenge"]

    saved = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (saved["client_id"], saved["subject"], saved["email"]) == (CLIENT, "user-1", "o@example.com")
    assert saved["ext_agent_host_id"] == seen["ext_agent_host_id"]
    assert "chatgpt.tokens.use.direct" in saved["scopes"]
    assert Auth(path, server.endpoints).status() == "ok"

    # Reauthorization reuses the issued client and the host id, without the name hint.
    seen2, open_browser2 = browser_consent(server)
    login(path, server.endpoints, open_browser=open_browser2, port=0)
    assert seen2["client_id"] == CLIENT and "agent_name_hint" not in seen2
    assert seen2["ext_agent_host_id"] == seen["ext_agent_host_id"]
    assert seen2["id_token_hint"] == saved["id_token"]


def test_login_rejects_id_token_with_wrong_nonce(tmp_path, server):
    path = tmp_path / "credentials.json"
    server.nonce_override = "attacker"
    _, open_browser = browser_consent(server)
    with pytest.raises(LoginError):
        login(path, server.endpoints, open_browser=open_browser, port=0)
    assert "access_token" not in json.loads(path.read_text())
