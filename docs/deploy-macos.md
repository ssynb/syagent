# Deploy on macOS

Verified on Apple silicon (M4, macOS 27) with Colima 0.10.3, Docker CLI and uv.

## 1. Create the sandbox VM

The agent's containers run in a dedicated Colima profile with no host mounts, so even a container escape lands in a VM that cannot see your files.

```bash
colima start syagent --cpu 2 --memory 4 --disk 30 --mount none --edit
```

In the editor that opens, replace `provision: null` with the block below, then save. The current Colima Ubuntu image has no `systemd-resolved`, which leaves `/etc/resolv.conf` dangling and breaks image pulls; `--dns` does not fix it.

```yaml
provision:
  - mode: system
    script: |
      [ -e /etc/resolv.conf ] || { rm -f /etc/resolv.conf; echo "nameserver 192.168.5.2" > /etc/resolv.conf; }
```

Done when `docker --context colima-syagent run --rm alpine nslookup api.openai.com` resolves.

## 2. Sign in with ChatGPT

```bash
cd host
uv run syagent login
```

Your browser opens the OpenAI consent page. Allow ChatGPT plan usage for syagent. Credentials land in `~/.syagent/credentials.json` (mode `0600`); the refresh token never leaves the host.

## 3. Serve

```bash
uv run syagent serve
```

On every start this builds the images when `seed/` or `sandbox/` changed, applies the egress firewall, then starts the agent container. An existing `/agent` is never overwritten: it belongs to the agent. Open <http://127.0.0.1:8000>.

| Variable | Default | Meaning |
|---|---|---|
| `SYAGENT_HOME` | `~/.syagent` | Credentials, `syagent.db`, delivered files |
| `SYAGENT_DOCKER_CONTEXT` | `colima-syagent` on macOS | Docker daemon that hosts the sandbox |
| `SYAGENT_AGENT_PORT` | `7700` | Host port the agent is published on (`127.0.0.1` only) |

## 4. Check the fence

Run from the Mac; every line except the first must fail.

```bash
D="docker --context colima-syagent exec syagent"
$D curl -sS -o /dev/null -w '%{http_code}\n' https://api.openai.com   # any HTTP status = internet works
$D curl -m 3 -sS http://$(ipconfig getifaddr en0):8000               # your Mac
$D curl -m 3 -sS http://$(route -n get default | awk '/gateway/{print $2}') # your router
$D curl -m 3 -sS telnet://192.168.5.2:22                              # the VM host gateway
$D iptables -F                                                        # no NET_ADMIN in the container
```

## Operating

- The agent stops replying: in the web app open the agent panel and try, in order, restart → roll back to the last healthy commit → restore a snapshot.
- The web app says you are signed out: run `uv run syagent login` again.
- Pause everything: stop `syagent serve`, then `colima stop syagent`. The container does not start on its own; it only runs under `serve`, after the firewall is in place.
