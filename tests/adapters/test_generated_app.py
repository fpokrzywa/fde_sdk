from importlib.resources import files
from types import SimpleNamespace


def test_generated_health_route_uses_real_sdk_serialization(monkeypatch):
    import databricks.sdk
    from databricks.sdk.service.sql import StatementExecutionAPI

    requests = []

    class API:
        _cfg = SimpleNamespace(workspace_id=None)

        def do(self, method, path, **kwargs):
            requests.append((method, path, kwargs["body"]))
            return {"statement_id": "statement-123", "status": {"state": "SUCCEEDED"}}

    statements = StatementExecutionAPI(API())
    monkeypatch.setattr(
        databricks.sdk, "WorkspaceClient", lambda: SimpleNamespace(statement_execution=statements)
    )
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "warehouse-123")
    source = files("shifde_databricks").joinpath("templates/app.py.txt").read_text()
    namespace = {"__name__": "generated_app_test"}
    exec(compile(source, "generated_app.py", "exec"), namespace)
    response = namespace["app"].test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "warehouse": "ok"}
    assert requests[0][2] == {
        "statement": "SELECT 1",
        "warehouse_id": "warehouse-123",
        "wait_timeout": "10s",
        "on_wait_timeout": "CANCEL",
    }
