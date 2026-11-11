from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from uuid import uuid4

from shifde_contracts.common import Target
from shifde_snowflake.clients import SnowflakeClient


def client():
    target = Target(
        tenant_id=uuid4(), connection_id=uuid4(), host="https://org-account.snowflakecomputing.com"
    )
    return SnowflakeClient(
        target,
        lambda: {
            "bearer": "native-oauth-secret",
            "snowflake": {
                "host": "org-account.snowflakecomputing.com",
                "account": "org-account",
                "role": "DEPLOY_ROLE",
            },
        },
        cli="snow",
    )


async def test_connector_normalizes_native_metadata_and_uses_explicit_oauth(monkeypatch):
    options = {}

    class Connection:
        sfqid = "query-id-123"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self, cursor_type):
            return self

        def execute(self, statement, **kwargs):
            assert statement.startswith("SELECT") and kwargs["timeout"] == 120

        def fetchmany(self, size):
            assert size == 1001
            return [
                {
                    "CREATED_ON": datetime(2026, 1, 1, tzinfo=UTC),
                    "VALIDATION": {"status": "SUCCESS", "errors": []},
                }
            ]

    def connect(**kwargs):
        options.update(kwargs)
        return Connection()

    monkeypatch.setattr("snowflake.connector.connect", connect)
    result = await client().execute("SELECT CURRENT_ACCOUNT()", warehouse="APP_WH")
    assert result["rows"][0]["validation"]["status"] == "SUCCESS"
    assert result["rows"][0]["created_on"].endswith("+00:00")
    assert options["authenticator"] == "oauth" and options["token"] == "native-oauth-secret"
    assert options["role"] == "DEPLOY_ROLE" and options["warehouse"] == "APP_WH"
    assert options["session_parameters"]["TIMEZONE"] == "UTC" and options["ocsp_fail_open"] is False


def test_cli_uses_primitive_stage_copy_without_hooks_force_or_secret_argv(monkeypatch):
    observed = []

    def run(argv, **kwargs):
        assert "native-oauth-secret" not in repr(argv) + repr(kwargs["env"])
        assert "--force" not in argv and "app" not in argv
        if "--version" in argv:
            return CompletedProcess(argv, 0, stdout="Snowflake CLI version: 3.33.0", stderr="")
        assert "--no-overwrite" in argv and "--no-auto-compress" in argv and "--no-refresh" in argv
        token_file = Path(argv[argv.index("--token-file-path") + 1])
        assert token_file.read_text() == "native-oauth-secret"
        assert "DATABRICKS_TOKEN" not in kwargs["env"] and "SNOWFLAKE_PASSWORD" not in kwargs["env"]
        observed.append(argv)
        return CompletedProcess(argv, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr("shifde_snowflake.clients.subprocess.run", run)
    client()._copy("/tmp/prepared/manifest.yml", '@"PKG"."SHIFDE"."RELEASES"/digest/')
    assert len(observed) == 1
