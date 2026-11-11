"""Deterministic HTTP boundary for real Fabric REST client and adapters."""

import copy
import json
from uuid import uuid4

import httpx


class FakeFabric:
    def __init__(self):
        self.items, self.operations, self.jobs = {}, {}, {}
        self.capacity = True
        self.lro_types = {"Notebook", "Ontology"}
        self.fail_poll = self.lose_response = False
        self.job_status = "Completed"
        self.requests = []
        self.no_update_result = False
        self.fail_item_get = False
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, copy.deepcopy(body)))
        assert request.headers["Authorization"].startswith("Bearer ")
        tokens = path.strip("/").split("/")
        if tokens[1] == "operations":
            op = self.operations[tokens[2]]
            if self.fail_poll:
                return httpx.Response(503)
            if len(tokens) == 4 and self.no_update_result and not op.get("create"):
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=op["result"] if len(tokens) == 4 else {"status": "Succeeded"},
                headers={"Retry-After": "0"},
            )
        workspace = tokens[2]
        if len(tokens) == 3:
            return httpx.Response(
                200,
                json={
                    "id": workspace,
                    "capacityId": str(uuid4())
                    if False
                    else "test-capacity"
                    if self.capacity
                    else None,
                },
            )
        collection = tokens[3]
        if collection == "items" and len(tokens) == 4:
            return httpx.Response(
                200,
                json={
                    "value": [
                        copy.deepcopy(i)
                        for i in self.items.values()
                        if i["workspaceId"] == workspace
                    ]
                },
            )
        if collection == "items" and len(tokens) > 5:
            if method == "POST":
                job = str(uuid4())
                self.jobs[job] = {"status": self.job_status}
                return httpx.Response(
                    202,
                    headers={
                        "Location": f"https://api.fabric.microsoft.com/v1/workspaces/{workspace}/items/{tokens[4]}/jobs/instances/{job}",
                        "Retry-After": "0",
                    },
                )
            return httpx.Response(200, json=self.jobs[tokens[-1]])
        item_type = {"lakehouses": "Lakehouse", "notebooks": "Notebook", "ontologies": "Ontology"}[
            collection
        ]
        if len(tokens) == 4 and method == "POST":
            if body.get("creationPayload", {}).get("enableSchemas") is False:
                return httpx.Response(400)
            if any(
                i["displayName"] == body["displayName"] and i["workspaceId"] == workspace
                for i in self.items.values()
            ):
                return httpx.Response(409)
            identity = str(uuid4())
            self.items[identity] = {
                "id": identity,
                "workspaceId": workspace,
                "type": item_type,
                "displayName": body["displayName"],
                "description": body.get("description", ""),
                "definition": copy.deepcopy(body.get("definition")),
            }
            result = copy.deepcopy(self.items[identity])
            if self.lose_response:
                raise httpx.ReadTimeout("lost acceptance")
            return self.accept(result, item_type, create=True)
        identity = tokens[4]
        if identity not in self.items:
            return httpx.Response(404)
        item = self.items[identity]
        if len(tokens) == 5:
            if method == "GET" and self.fail_item_get:
                return httpx.Response(503)
            if method == "PATCH":
                item.update(body)
            return httpx.Response(200, json=copy.deepcopy(item))
        if tokens[5] == "getDefinition":
            return httpx.Response(200, json={"definition": copy.deepcopy(item["definition"])})
        if tokens[5] == "updateDefinition":
            item["definition"] = copy.deepcopy(body["definition"])
            return self.accept({}, item_type)
        raise AssertionError("unhandled native request")

    def accept(self, result, item_type, create=False):
        if item_type not in self.lro_types:
            return httpx.Response(201 if create else 200, json=result)
        identity = str(uuid4())
        self.operations[identity] = {"result": result, "create": create}
        return httpx.Response(
            202,
            headers={
                "x-ms-operation-id": identity,
                "Location": "https://untrusted.example/redirect",
                "Retry-After": "0",
            },
        )
