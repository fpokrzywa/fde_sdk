"""Fresh Docker job on an operator-attested dedicated tenant VM in production."""

import json
import os
import subprocess
import threading
import time
from uuid import uuid4

from shifde_contracts.common import utcnow

MAX_FRAME = 32 * 1024 * 1024


def process_environment():
    # Native secrets, worker tokens and application variables never reach Docker/Cosign.
    names = (
        "PATH",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "SSL_CERT_FILE",
    )
    return {k: os.environ[k] for k in names if k in os.environ}


class DockerSandbox:
    def __init__(self, settings):
        self.settings = settings

    def command(self, job, name):
        if (
            job.image != self.settings.image
            or job.tenant_id != self.settings.tenant_id
            or job.pool_id != self.settings.pool_id
        ):
            raise PermissionError("job does not match this supervisor")
        network = (
            "none"
            if job.action in {"build", "validate", "scaffold"}
            else self.settings.native_network
        )
        if job.action not in {"build", "validate", "scaffold"} and network == "none":
            raise ValueError("native jobs require a restricted egress network")
        return [
            self.settings.docker,
            "run",
            "--name",
            name,
            "--interactive",
            "--pull",
            "never",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "128",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--network",
            network,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
            "--log-driver",
            "none",
            "--stop-timeout",
            "5",
            "--entrypoint",
            "/opt/venv/bin/python",
            job.image,
            "-m",
            "shifde_executor.runner",
        ]

    def verify_image(self):
        if self.settings.isolation != "development":
            subprocess.run(
                [
                    self.settings.cosign,
                    "verify",
                    "--key",
                    str(self.settings.cosign_key),
                    self.settings.image,
                ],
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=process_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def _remove(self, name):
        subprocess.run(
            [self.settings.docker, "rm", "--force", name],
            timeout=30,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=process_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def run(self, job, handler, *, stop=None):
        self.verify_image()
        name = "shifde-job-" + job.id.hex + "-" + uuid4().hex[:8]
        seconds = min(self.settings.max_job_seconds, (job.deadline - utcnow()).total_seconds())
        if seconds <= 0:
            raise TimeoutError("job deadline expired")
        until = time.monotonic() + seconds
        process = subprocess.Popen(
            self.command(job, name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=process_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        done = threading.Event()

        def monitor():
            while not done.wait(0.1):
                if time.monotonic() >= until or stop is not None and stop.is_set():
                    self._remove(name)
                    process.kill()
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(job.model_dump_json() + "\n")
            process.stdin.flush()
            while True:
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    raise RuntimeError("sandbox protocol failed")
                message = json.loads(line)
                if "result" in message:
                    process.stdin.close()
                    # A result can precede Docker's attached-process exit. Both
                    # phases share the job budget; there is no shorter exit SLA.
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("sandbox job deadline expired")
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        raise TimeoutError("sandbox job deadline expired") from None
                    if process.returncode:
                        raise RuntimeError("sandbox failed")
                    return message["result"]
                try:
                    value = handler(message["rpc"], message.get("data", {}))
                    response = {"ok": True, "value": value}
                except Exception:
                    response = {"ok": False}
                process.stdin.write(json.dumps(response) + "\n")
                process.stdin.flush()
        finally:
            done.set()
            thread.join(timeout=35)
            self._remove(name)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
