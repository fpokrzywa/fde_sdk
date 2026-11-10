"""Outbound-only supervisor. Local native credentials never enter gateway records."""

import base64
import threading
import time

from shifde_core.artifact_store import MAX_ARCHIVE_BYTES

from shifde_executor.client import GatewayClient
from shifde_executor.config import ExecutorSettings
from shifde_executor.credentials import LocalCredentialBroker
from shifde_executor.receipts import ReceiptOutbox
from shifde_executor.sandbox import DockerSandbox


class Supervisor:
    def __init__(self, settings, *, client=None, sandbox=None, broker=None):
        self.settings = settings
        self.client = client or GatewayClient(settings)
        self.sandbox = sandbox or DockerSandbox(settings)
        self.broker = broker or LocalCredentialBroker(settings)
        self.receipts = ReceiptOutbox(settings)

    def run_once(self):
        self.receipts.replay(self.client)
        lease = self.client.lease()
        if lease is None:
            return False
        job = lease.job
        if (
            job.tenant_id != self.settings.tenant_id
            or job.pool_id != self.settings.pool_id
            or job.image != self.settings.image
        ):
            raise PermissionError("gateway job does not match supervisor")
        stop, done = threading.Event(), threading.Event()

        def heartbeat():
            while not done.wait(self.settings.heartbeat_seconds):
                try:
                    if self.client.call(lease, "heartbeats")["cancel_requested"]:
                        stop.set()
                        return
                except Exception:
                    stop.set()
                    return

        def rpc(method, data):
            pending = None
            if method == "checkpoint" and data.get("phase") == "complete":
                # Retain native evidence even when cancellation/disconnect arrived meanwhile.
                pending = self.receipts.remember(lease, data)
            if stop.is_set():
                raise RuntimeError("lease is no longer available")
            if method == "read":
                return {
                    "bytes": base64.b64encode(self.client.read(lease, data["source_id"])).decode()
                }
            if method == "put":
                raw = base64.b64decode(data["bytes"], validate=True)
                if len(raw) > MAX_ARCHIVE_BYTES:
                    raise ValueError("artifact exceeds limit")
                return self.client.put(lease, raw)
            if method == "checkpoint":
                value = self.client.call(lease, "checkpoints", data)
                self.receipts.acknowledge(pending)
                return value
            if method == "guard":
                return self.client.call(lease, "guards")
            if method == "credential":
                grant = self.client.grant(lease)
                handle = self.broker.resolve(grant, job)
                return {"bearer": handle.bearer.get_secret_value()}
            raise PermissionError("unknown executor RPC")

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            try:
                result = self.sandbox.run(job, rpc, stop=stop)
            except Exception:
                result = {"status": "failed", "error_code": "SANDBOX_FAILED"}
            # Failure to persist a result is not permission to execute again.
            if not self.receipts.has_pending(job.id):
                self.client.call(lease, "results", result)
        finally:
            done.set()
            thread.join(timeout=35)
        return True

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception:
                # No native exception/HTTP/body text or secret values in supervisor logs.
                print("executor iteration failed; gateway retains job authority", flush=True)
            time.sleep(self.settings.poll_seconds)


def main():
    settings = ExecutorSettings()
    Supervisor(settings).run()


if __name__ == "__main__":
    main()
