"""Docker driver for the agent sandbox: images, egress firewall, container, file transfer, git, snapshots."""

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

NETWORK = "syagent"
BRIDGE = "syagent0"
SUBNET = "10.213.0.0/24"
CONTAINER = "syagent"
AGENT_PORT = 7000
AGENT_REPO = "syagent-agent"
FIREWALL_REPO = "syagent-firewall"
SNAPSHOT_REPO = "syagent-snapshot"
# image repository → (Dockerfile, build-context paths relative to the repository root)
IMAGES = {
    FIREWALL_REPO: ("sandbox/firewall.Dockerfile", ["sandbox/firewall.Dockerfile", "sandbox/firewall.sh"]),
    AGENT_REPO: ("sandbox/agent.Dockerfile", ["sandbox/agent.Dockerfile", "sandbox/agent-entrypoint.sh", "seed"]),
}
SKIP_NAMES = {"__pycache__", ".pytest_cache"}
SKIP_PATHS = ("seed/tests",)  # the seed's own tests are not part of the agent
TAG_LENGTH = 12
ATOMIC_WRITE = 'set -e; mkdir -p "$(dirname "$1")"; cat > "$1.tmp"; mv "$1.tmp" "$1"'


class SandboxError(Exception):
    pass


def context_files(repo: Path, paths: list[str]) -> list[str]:
    """The files of an image's build context, relative to the repository root, sorted."""
    files = []
    for path in paths:
        root = repo / path
        for file in [root] if root.is_file() else root.rglob("*"):
            rel = file.relative_to(repo).as_posix()
            if file.is_file() and not SKIP_NAMES & set(rel.split("/")) and not rel.startswith(SKIP_PATHS):
                files.append(rel)
    return sorted(files)


def content_tag(repo: Path, files: list[str]) -> str:
    """Image tag derived from the build context, so any change to it yields a new image."""
    digest = hashlib.sha256()
    for rel in files:
        data = (repo / rel).read_bytes()
        digest.update(f"{rel}\0{(repo / rel).stat().st_mode & 0o111:o}\0{len(data)}\0".encode() + data)
    return digest.hexdigest()[:TAG_LENGTH]


def build_context(repo: Path, files: list[str]) -> bytes:
    """Tar of just the files an image needs (the docker CLI may lack BuildKit's per-Dockerfile ignore)."""
    buf = io.BytesIO()
    # GNU format: a leading PAX header makes the legacy builder mistake the stream for a Dockerfile.
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel in files:
            tar.add(repo / rel, arcname=rel)
    return buf.getvalue()


class Sandbox:
    def __init__(self, context: str | None, repo: Path, host_port: int):
        self.base = ["docker", *(["--context", context] if context else [])]
        self.repo = repo
        self.host_port = host_port
        self.contexts = {name: context_files(repo, paths) for name, (_, paths) in IMAGES.items()}
        self.images = {name: f"{name}:{content_tag(repo, files)}" for name, files in self.contexts.items()}

    def docker(self, *args: str, input: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
        proc = subprocess.run([*self.base, *args], input=input, capture_output=True, check=False)
        if proc.returncode not in ok:
            raise SandboxError(f"docker {args[0]} failed ({proc.returncode}): {proc.stderr.decode().strip()}")
        return proc.stdout

    def has_image(self, ref: str) -> bool:
        return subprocess.run([*self.base, "image", "inspect", ref], capture_output=True, check=False).returncode == 0

    def up(self) -> None:
        """Everything `serve` needs before the agent may run, in the order that keeps it fenced."""
        for name, (dockerfile, _) in IMAGES.items():
            image = self.images[name]
            if not self.has_image(image):
                print(f"building {image} ...", flush=True)
                self.docker("build", "-q", "-f", dockerfile, "-t", image, "-",
                            input=build_context(self.repo, self.contexts[name]))
        if not self.docker("network", "ls", "-q", "--filter", f"name=^{NETWORK}$").strip():
            self.docker("network", "create", "--driver", "bridge", "--subnet", SUBNET, "--ipv6=false",
                        "-o", f"com.docker.network.bridge.name={BRIDGE}", NETWORK)
        self.ensure_running()

    def apply_firewall(self) -> None:
        self.docker("run", "--rm", "--privileged", "--net=host", self.images[FIREWALL_REPO], BRIDGE)

    def ensure_running(self) -> None:
        """Firewall first, then the container. Docker never starts it by itself (restart policy "no"),
        so after a VM reboot it stays down until rules are back."""
        self.apply_firewall()
        state = self.docker("container", "inspect", "-f", "{{.State.Running}}", CONTAINER, ok=(0, 1)).strip()
        if not state:
            self._run(self.images[AGENT_REPO])  # an existing container (and its /agent) is kept as is
        elif state != b"true":
            self.docker("start", CONTAINER)

    def _run(self, image: str) -> None:
        self.docker("run", "-d", "--name", CONTAINER, "--hostname", CONTAINER, "--network", NETWORK, "--init",
                    "-p", f"127.0.0.1:{self.host_port}:{AGENT_PORT}", "--restart", "no", image)

    def recreate(self, image: str) -> None:
        if not self.has_image(image):
            raise SandboxError(f"no such image: {image}")
        self.docker("rm", "-f", CONTAINER)
        self.apply_firewall()
        self._run(image)

    def exec(self, *cmd: str, input: bytes | None = None, ok: tuple[int, ...] = (0,)) -> str:
        return self.docker("exec", *(["-i"] if input is not None else []), CONTAINER, *cmd,
                           input=input, ok=ok).decode()

    def write(self, path: str, data: bytes) -> None:
        """Atomically replace a file inside the container (readers never see a partial file)."""
        self.exec("sh", "-c", ATOMIC_WRITE, "sh", path, input=data)

    def copy_out(self, path: str) -> bytes:
        """Content of a regular file in the container; links and directories are refused."""
        with tarfile.open(fileobj=io.BytesIO(self.docker("cp", f"{CONTAINER}:{path}", "-"))) as tar:
            member = tar.next()
            if member is None or not member.isfile():
                raise SandboxError(f"{path} is not a regular file")
            return tar.extractfile(member).read()

    def kill_agent(self) -> None:
        """Kill the agent process; the container's supervisor loop starts it again."""
        self.exec("pkill", "-f", "/agent/main.py", ok=(0, 1))

    def commit(self, tag: str) -> None:
        self.docker("commit", CONTAINER, f"{SNAPSHOT_REPO}:{tag}")

    def snapshot_tags(self) -> list[str]:
        out = self.docker("image", "ls", "--format", "{{.Tag}}", SNAPSHOT_REPO).decode()
        return out.split()
