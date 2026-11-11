"""Explicit opt-in native writes through an existing hosted API and isolated workers."""

import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.live


@pytest.mark.parametrize("iq", [False, True])
def test_live_fabric_create_update_and_execute(iq):
    if os.getenv("SHIFDE_LIVE_FABRIC") != "1":
        pytest.skip("Fabric live acceptance is not explicitly enabled")
    target_name = "SHIFDE_LIVE_FABRIC_IQ_TARGET_ID" if iq else "SHIFDE_LIVE_FABRIC_TARGET_ID"
    required = ["SHIFDE_LIVE_API_URL", "SHIFDE_LIVE_API_TOKEN_FILE", target_name]
    assert all(os.getenv(key) for key in required), (
        "enabled Fabric acceptance requires API URL, refreshed token file and both native target IDs"
    )
    kind = "fabric.iq/v1" if iq else "fabric.solution/v1"
    with httpx.Client(
        base_url=os.environ[required[0]], timeout=30, follow_redirects=False, trust_env=False
    ) as client:

        def request(method, path, body=None):
            token = Path(os.environ[required[1]]).read_text().strip()
            response = client.request(
                method,
                path,
                json=body,
                headers={"Authorization": "Bearer " + token, "Idempotency-Key": uuid4().hex},
            )
            assert response.is_success, f"Fabric live API returned {response.status_code}"
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
                }, f"Native acceptance needs attention: {operation['id']}"
                time.sleep(2)
            pytest.fail("Fabric live operation exceeded deadline")

        app = request(
            "POST", "/v1/applications", {"name": "fabric-live-" + uuid4().hex, "kind": kind}
        ).json()
        items = [
            {"key": "lake", "item_type": "Lakehouse", "definition_path": "lake.json"},
            {
                "key": "notebook",
                "item_type": "Notebook",
                "definition_path": "main.ipynb",
                "depends_on": ["lake"],
                "lakehouse": "lake",
            },
        ]
        if iq:
            items.append(
                {
                    "key": "ontology",
                    "item_type": "Ontology",
                    "definition_path": "ontology",
                    "depends_on": ["notebook"],
                }
            )
        spec = {"kind": kind, "items": items, "verification_notebook": "notebook"}
        previous = None
        for _ in range(2):
            source = submit("scaffolds", {"application_id": app["id"], "spec": spec})
            project = request(
                "POST",
                "/v1/projects",
                {"application_id": app["id"], "spec": spec, "source_id": source["id"]},
            ).json()
            artifact = submit("builds", {"project_id": project["id"]})
            plan = submit(
                "plans", {"artifact_id": artifact["id"], "target_id": os.environ[target_name]}
            )
            result = submit("deployments", {"artifact_id": artifact["id"], "plan_id": plan["id"]})
            assert result["healthy"] and (previous is None or result["resources"] == previous)
            previous = result["resources"]
