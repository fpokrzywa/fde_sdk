import re
import time
from uuid import uuid4

import httpx
from shifde_contracts.jobs import CredentialGrant, JobLease


class GatewayClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.gateway_url + "/internal/v1/",
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    def close(self):
        self.http.close()

    def _request(self, method, path, *, body=None, content=None, lease=None):
        headers = {
            "Authorization": "Bearer "
            + self.settings.token_file.read_text(encoding="utf-8").strip()
        }
        if lease is not None:
            headers.update(
                {"X-Lease-Id": str(lease.lease_id), "X-Execution-Fence": str(lease.fence)}
            )
        # Only replay byte-identical protocol requests after transport uncertainty.
        for attempt in range(3):
            try:
                response = self.http.request(
                    method, path, headers=headers, json=body, content=content
                )
                if response.status_code >= 400:
                    code = "REQUEST_FAILED"
                    try:
                        candidate = response.json()["error"]["code"]
                        if isinstance(candidate, str) and re.fullmatch(r"[A-Z_]{1,64}", candidate):
                            code = candidate
                    except (ValueError, KeyError, TypeError):
                        pass
                    raise RuntimeError(
                        f"gateway rejected executor request ({response.status_code}, {code})"
                    )
                return response
            except (httpx.ConnectError, httpx.ReadTimeout):
                if attempt == 2:
                    raise RuntimeError("gateway unavailable") from None
                time.sleep(0.1 * 2**attempt)
        raise RuntimeError("gateway unavailable")

    def lease(self):
        value = self._request(
            "POST", "job-leases", body={"image": self.settings.image, "request_id": str(uuid4())}
        ).json()
        return JobLease.model_validate(value) if value else None

    def call(self, lease, method, body=None):
        payload = {"lease_id": str(lease.lease_id), "fence": lease.fence, **(body or {})}
        return self._request("POST", f"execution-jobs/{lease.job.id}/{method}", body=payload).json()

    def grant(self, lease):
        return CredentialGrant.model_validate(self.call(lease, "credential-grants"))

    def replay_checkpoint(self, identity, body):
        return self._request("POST", f"execution-jobs/{identity}/checkpoints", body=body).json()

    def read(self, lease, source_id):
        return self._request(
            "GET", f"execution-jobs/{lease.job.id}/artifacts/{source_id}", lease=lease
        ).content

    def put(self, lease, data):
        return self._request(
            "POST", f"execution-jobs/{lease.job.id}/artifacts", content=data, lease=lease
        ).json()
