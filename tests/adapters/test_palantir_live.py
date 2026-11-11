"""Opt-in acceptance against a preconfigured hosted API and real Foundry workers."""

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from shifde_contracts.projects import parse_spec

pytestmark = pytest.mark.live


def test_live_palantir_install_and_upgrade_prepublished_versions():
    if os.getenv("SHIFDE_LIVE_PALANTIR") != "1":
        pytest.skip("Foundry live acceptance is not explicitly enabled")
    keys = [
        "SHIFDE_LIVE_API_URL",
        "SHIFDE_LIVE_API_TOKEN_FILE",
        "SHIFDE_LIVE_PALANTIR_TARGET_ID",
        "SHIFDE_LIVE_PALANTIR_SPECS_FILE",
    ]
    assert all(os.getenv(key) for key in keys), (
        "Foundry acceptance requires hosted API, refreshed token file, target ID and two approved descriptors"
    )
    specs = [parse_spec(s) for s in json.loads(Path(os.environ[keys[3]]).read_text())]
    assert len(specs) == 2 and all(s.kind == "palantir.marketplace/v1" for s in specs)
    assert specs[0].version_id != specs[1].version_id
    with httpx.Client(
        base_url=os.environ[keys[0]], timeout=30, follow_redirects=False, trust_env=False
    ) as client:

        def request(method, path, body=None):
            response = client.request(
                method,
                path,
                json=body,
                headers={
                    "Authorization": "Bearer " + Path(os.environ[keys[1]]).read_text().strip(),
                    "Idempotency-Key": uuid4().hex,
                },
            )
            assert response.is_success, f"Foundry live hosted API returned {response.status_code}"
            return response

        def submit(route, body):
            response = request("POST", "/v1/" + route, body)
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                operation = request("GET", response.headers["Location"]).json()
                if operation["state"] == "succeeded":
                    return operation["result"]["value"]
                assert operation["state"] not in {
                    "failed",
                    "needs_attention",
                    "cancelled",
                    "waiting_for_approval",
                }, f"Foundry acceptance needs attention: {operation['id']}"
                time.sleep(2)
            pytest.fail(
                "Foundry live operation exceeded deadline; inspect native IDs before retrying"
            )

        app = request(
            "POST",
            "/v1/applications",
            {"name": "palantir-live-" + uuid4().hex, "kind": "palantir.marketplace/v1"},
        ).json()
        resources = None
        for spec in specs:
            source = submit(
                "scaffolds", {"application_id": app["id"], "spec": spec.model_dump(mode="json")}
            )
            project = request(
                "POST",
                "/v1/projects",
                {
                    "application_id": app["id"],
                    "spec": spec.model_dump(mode="json"),
                    "source_id": source["id"],
                },
            ).json()
            artifact = submit("builds", {"project_id": project["id"]})
            plan = submit(
                "plans", {"artifact_id": artifact["id"], "target_id": os.environ[keys[2]]}
            )
            deployed = submit("deployments", {"artifact_id": artifact["id"], "plan_id": plan["id"]})
            assert deployed["healthy"] and (resources is None or deployed["resources"] == resources)
            resources = deployed["resources"]
