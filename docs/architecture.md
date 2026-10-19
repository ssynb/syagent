# Architecture

syagent is split into two parts with opposite trust levels.

| Part | Lives in | Who controls it | Contents |
|---|---|---|---|
| **Host** (`host/`) | The owner's machine, outside any sandbox | The owner | Web chat, records (SQLite), ChatGPT credentials, watchdog, rollback, snapshots, egress firewall |
| **Agent** (`seed/` → `/agent` in the sandbox) | A Docker container inside a dedicated Linux VM | The agent itself | Its own code, tools, prompt, and the whole container filesystem (it runs as root) |

The agent may rewrite anything inside its container. Everything the owner relies on to judge, steer and recover it stays on the host side, where the agent has no write path.

```
browser ──HTTP──▶ host (127.0.0.1:8000) ──docker exec / docker cp──▶ sandbox container
                     │  ▲                                                  │
                     │  └──── POST /turn (SSE), GET /health ───────────────┘  (127.0.0.1:7700, host → agent only)
                     ▼
                ~/.syagent  (credentials, syagent.db, files/)
```

## Hard boundaries

1. The container has no bind mounts and no host credentials. On macOS the VM itself is a dedicated Colima profile started with `--mount none`.
2. Egress: the container may reach the public internet. Every private, link-local and CGNAT range (which covers the host, the VM, the LAN and the router) is dropped, and so is any new connection from the container to the VM itself. Rules live in the Docker daemon's network namespace, applied by the host through a privileged helper container; the agent container is unprivileged and cannot change them.
3. There is no network path from the agent to the host. The host pushes what the agent needs as files:
   - `/run/syagent/token` — `{"access_token": "...", "expires_at": "<ISO8601>"}`, rewritten after every refresh.
   - `/run/syagent/records.json` — full export of conversations, messages and scores, rewritten after every turn. The original lives in SQLite on the host; the agent only ever sees a copy.
4. The ChatGPT refresh token never enters the container. Restoring a snapshot therefore cannot roll credentials back to a revoked refresh token.
5. The host can always recover the agent: restart the process, reset `/agent` to the last healthy commit, or recreate the container from a snapshot image.

## Agent contract (frozen)

This is the only interface between host and agent. The agent may change everything else about itself.

The agent listens on port `7000` inside the container, published to `127.0.0.1:7700` on the host (macOS reserves 7000 for AirPlay).

`GET /health` → `200 {"commit": "<git HEAD of /agent when this process started>"}`. The host remembers the last commit that answered healthy, so a self-edit only counts as healthy after the agent restarts onto it.

`POST /turn` → `text/event-stream`. Request body is one of:

```json
{"conversation_id": "c1", "message_id": "m1", "reply_id": "m2", "kind": "message", "text": "...", "attachments": ["/inbox/m1/photo.jpg"]}
{"conversation_id": "c1", "message_id": "m3", "reply_id": "m4", "kind": "feedback", "target_message_id": "m2", "score": 2, "text": "owner's critique"}
```

`message_id` is the owner's message, `reply_id` the id the host assigned to the agent reply this turn produces, so a later feedback's `target_message_id` can be matched to it. `score` is 1–5 or `null`. Response events, one JSON object per `data:` line:

```json
{"type": "text", "delta": "..."}
{"type": "file", "path": "/outbox/m1/result.xlsx"}
{"type": "done"}
{"type": "error", "message": "..."}
```

Rules:

- The host sends turns for one conversation strictly one at a time.
- Attachments are copied by the host into `/inbox/<message_id>/` before the turn starts. Files the agent wants to deliver go to `/outbox/<message_id>/` and are announced with a `file` event; the host copies them out after `done`.
- Feedback goes through `/turn` too, so the agent's reflection and the list of changes it made stream back into the chat.
- Restart: the agent creates `/run/syagent/restart`; once no turn is in flight, the process exits and the container's supervisor loop starts it again.

### records.json

```json
{"conversations": [{"id": "c1", "title": "...", "messages": [
  {"id": "m1", "role": "owner", "kind": "message", "text": "...", "files": ["photo.jpg"], "target_message_id": null, "score": null, "status": "done", "created_at": "<ISO8601>"}
]}]}
```

`role` is `owner` or `agent`; `kind` is `message` or `feedback`; `status` is `done` or `error`. On an agent reply, `score` is the latest non-null score of the feedback targeting it; on a feedback message it is the score given. A failed reply carries the error detail in `text`.

## Seed

The seed is what `/agent` contains on day zero. It is deliberately tiny so the agent can read all of itself.

- `main.py` — stdlib `ThreadingHTTPServer` serving the contract, plus the conversation loop: read the token file, call the Responses API with the official `openai` SDK (`store: false`, `stream: true`, encrypted reasoning carried between steps), execute tool calls, stream text.
- One built-in tool, `run`: `bash -lc <command>` with a timeout, returning exit code, stdout and stderr (truncated past a limit).
- Tool loading: before every model call, each `/agent/tools/*.py` exporting `SPEC` (a Responses function tool) and `run(args: dict) -> str` is loaded. A file that fails to load is skipped and the error is shown to the model. Empty at birth.
- `prompt.md` — system prompt, re-read every turn.
- `config.toml` — model, reasoning effort, API base URL. An empty model means "first model listed for this account".
- Conversation state lives in `/agent/state/` (git-ignored).

`/agent` is a git repository. The agent commits every self-modification with the reason, and the message id when the change came from feedback.

## Host

- `syagent login` — Sign in with ChatGPT for open-source apps (dynamic client registration, PKCE, `127.0.0.1` loopback callback). Credentials are stored in `~/.syagent/credentials.json` with mode `0600`; the host id is generated once and persisted.
- `syagent serve` — rebuilds the images when `seed/` or `sandbox/` changed (an existing `/agent` is never overwritten), applies the egress firewall before every container start (the container has no Docker restart policy, so it never runs unfenced), refreshes the access token before expiry (serialised) and pushes it into the container, runs the watchdog (`/health` every 30 s, remembers the last healthy commit), and serves the web app on `127.0.0.1:8000`.

### Web API

| Method & path | Purpose |
|---|---|
| `GET /api/status` | `{"auth": "ok" \| "login_required", "agent": "online" \| "offline", "commit", "healthy_commit"}` |
| `GET /api/conversations` | List, newest first: `[{id, title, updated_at}]` |
| `POST /api/conversations` | Create → `{id}` |
| `GET /api/conversations/{id}/messages` | `[{id, role, kind, text, files, target_message_id, score, status, created_at}]`; `role` is `owner` or `agent`, `files` is `[{name, url}]`, `status` is `done` or `error` |
| `POST /api/conversations/{id}/messages` | Multipart: `kind`, `text`, `files[]`, and for feedback `target_message_id`, `score`. Responds with SSE: first `{"type":"ack","owner_message_id","agent_message_id"}`, then the agent's `text` events, `file` events rewritten to `{"type":"file","name","url"}`, then `done` or `error` |
| `GET /api/files/{message_id}/{name}` | Download an attachment or delivered file |
| `POST /api/agent/restart` | Restart the agent process |
| `GET /api/agent/commits` | Recent `/agent` history `[{commit, subject, date}]` |
| `POST /api/agent/rollback` | `{"commit": optional}`, defaults to the last healthy commit; the current HEAD is kept as branch `abandoned/<timestamp>` |
| `GET /api/agent/snapshots` | `[{tag, created_at}]` |
| `POST /api/agent/snapshots` | Commit the running container to a snapshot image |
| `POST /api/agent/snapshots/{tag}/restore` | Recreate the container from that image |
