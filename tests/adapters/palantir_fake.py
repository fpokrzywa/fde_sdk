"""Documented Foundry REST boundary. No platform adapter methods are mocked."""

import copy
import hashlib
import json
from uuid import uuid4

import httpx


def make_bindings():
    return {
        "marketplace": f"ri.marketplace.main.local.{uuid4()}",
        "product": str(uuid4()),
        "parent_folder": f"ri.compass.main.folder.{uuid4()}",
        "namespace": f"ri.compass.main.namespace.{uuid4()}",
        "ontology": f"ri.ontology.main.ontology.{uuid4()}",
        "frontend_origin": "https://native-app.example.test",
    }


def make_spec():
    from shifde_contracts.projects import parse_spec

    return parse_spec(
        {
            "kind": "palantir.marketplace/v1",
            "version_id": str(uuid4()),
            "object_type": "Example",
            "frontend_asset_path": "assets/health.txt",
            "frontend_asset_sha256": hashlib.sha256(b"published frontend asset").hexdigest(),
        }
    )


def approval(spec, bindings):
    return {
        **bindings,
        "version_id": str(spec.version_id),
        "object_type": spec.object_type,
        "frontend_asset_path": spec.frontend_asset_path,
        "frontend_asset_sha256": spec.frontend_asset_sha256,
    }


class FakePalantir:
    def __init__(self, bindings):
        self.bindings = bindings
        self.installations, self.jobs, self.requests = {}, {}, []
        self.status = "SUCCEEDED"
        self.schema_revision = 1
        self.frontend = b"published frontend asset"
        self.lose_registration = self.lose_job = self.fail_readback = False
        self.deny_objects = False
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, copy.deepcopy(body), dict(request.headers)))
        if request.url.host == "native-app.example.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.frontend)
        assert request.headers["Authorization"].startswith("Bearer ")
        if path.startswith("/api/v2/"):
            if "/objects/" in path:
                return (
                    httpx.Response(403)
                    if self.deny_objects
                    else httpx.Response(200, json={"data": [], "totalCount": "0"})
                )
            if "/objectTypes/" in path:
                return httpx.Response(
                    200,
                    json={"apiName": "Example", "properties": {}, "revision": self.schema_revision},
                )
            return httpx.Response(
                200, json={"rid": self.bindings["ontology"], "apiName": "example"}
            )
        assert request.url.params.get("preview") == "true"
        if path.endswith("marketplaceInstallations") and method == "POST":
            assert set(body) == {
                "marketplaceRid",
                "productId",
                "parentFolderRid",
                "installationSuffix",
                "targetVersionHint",
            }
            assert body["targetVersionHint"]["type"] == "versionId"
            identity = f"ri.marketplace.main.block-set-installation.{uuid4()}"
            value = {
                "rid": identity,
                "marketplaceRid": body["marketplaceRid"],
                "productId": body["productId"],
                "parentFolderRid": body["parentFolderRid"],
                "namespaceRid": self.bindings["namespace"],
            }
            self.installations[identity] = value
            if self.lose_registration:
                raise httpx.ReadError("lost registration response")
            return httpx.Response(200, json=copy.deepcopy(value))
        if "/marketplaceInstallations/" in path:
            return (
                httpx.Response(503)
                if self.fail_readback
                else httpx.Response(
                    200, json=copy.deepcopy(self.installations[path.rsplit("/", 1)[1]])
                )
            )
        if path.endswith("marketplaceInstallationJobs") and method == "POST":
            assert body["submissionMode"] == {
                "type": "submit",
                "errorTolerance": "NONE",
                "errorHandling": "ABORT",
            }
            (target,) = body["installations"]
            assert set(target) == {"installationRid", "targetVersion", "inputs"}
            assert target["inputs"] == {"values": {}, "explicitlyUnset": []}
            self.installations[target["installationRid"]]["latestTargetVersion"] = {
                "versionId": target["targetVersion"]["versionId"]
            }
            identity = f"ri.marketplace.main.installation-job.{uuid4()}"
            self.jobs[identity] = {"rid": identity, "status": self.status}
            if self.lose_job:
                raise httpx.ReadError("lost job acceptance")
            return httpx.Response(200, json=self.jobs[identity])
        if "/marketplaceInstallationJobs/" in path:
            return httpx.Response(200, json=self.jobs[path.rsplit("/", 1)[1]])
        raise AssertionError((method, path, body))
