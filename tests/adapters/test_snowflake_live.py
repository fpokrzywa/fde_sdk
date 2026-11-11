"""Opt-in acceptance against a deployed API and real enrolled Snowflake workers.

SHIFDE_LIVE_SNOWFLAKE=1 authorizes writes to the configured development target.
Resources remain for inspection; this test never deletes native objects.
"""

import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.live


def test_live_snowflake_package_install_patch_and_upgrade():
    if os.getenv("SHIFDE_LIVE_SNOWFLAKE") != "1":
        pytest.skip("explicit live Snowflake acceptance is not enabled")
    required = [
        "SHIFDE_LIVE_API_URL",
        "SHIFDE_LIVE_API_TOKEN_FILE",
        "SHIFDE_LIVE_SNOWFLAKE_TARGET_ID",
    ]
    missing = [name for name in required if not os.getenv(name)]
    assert not missing, "live acceptance configuration missing: " + ", ".join(missing)
    run = uuid4().hex
    with httpx.Client(
        base_url=os.environ[required[0]], timeout=30, follow_redirects=False, trust_env=False
    ) as client:

        def request(method, route, **kwargs):
            token = Path(os.environ[required[1]]).read_text().strip()
            response = client.request(
                method,
                route,
                headers={"Authorization": "Bearer " + token, "Idempotency-Key": run + uuid4().hex},
                **kwargs,
            )
            assert response.is_success, f"live API returned {response.status_code}"
            return response

        def submit(route, body):
            response = request("POST", "/v1/" + route, json=body)
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                operation = request("GET", response.headers["Location"]).json()
                if operation["state"] == "succeeded":
                    return operation["result"]["value"]
                assert operation["state"] not in {
                    "failed",
                    "cancelled",
                    "needs_attention",
                    "waiting_for_approval",
                }, f"live operation requires attention: {operation['id']} ({operation['state']})"
                time.sleep(2)
            pytest.fail("live operation exceeded acceptance deadline")

        app = request(
            "POST",
            "/v1/applications",
            json={"name": "live-snowflake-" + run, "kind": "snowflake.native-app/v1"},
        ).json()
        installed = None
        for version, patch in (("V1", 0), ("V1", 1), ("V2", 0)):
            spec = {
                "kind": "snowflake.native-app/v1",
                "package_binding": "package",
                "application_binding": "application",
                "warehouse_binding": "warehouse",
                "release_version": version,
                "release_patch": patch,
            }
            source = submit("scaffolds", {"application_id": app["id"], "spec": spec})
            project = request(
                "POST",
                "/v1/projects",
                json={"application_id": app["id"], "source_id": source["id"], "spec": spec},
            ).json()
            artifact = submit("builds", {"project_id": project["id"]})
            plan = submit(
                "plans", {"artifact_id": artifact["id"], "target_id": os.environ[required[2]]}
            )
            result = submit("deployments", {"artifact_id": artifact["id"], "plan_id": plan["id"]})
            native = next(r["native_id"] for r in result["resources"] if r["key"] == "application")
            assert result["healthy"] and (installed is None or native == installed)
            installed = native
