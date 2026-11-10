import os
import threading
import time
from typing import Any
from uuid import UUID

from databricks.sdk import WorkspaceClient
from shifde_contracts.hosted import Connection
from shifde_core.hashing import digest
from shifde_databricks.adapter import DatabricksAdapter
from shifde_databricks.clients import DatabricksCLI, DatabricksWorkspaceGateway
from shifde_databricks.source import ADAPTER_VERSION, CLI_VERSION

from shifde_hosted.config import Settings
from shifde_hosted.errors import Forbidden
from shifde_hosted.persistence import repository as r
from shifde_hosted.service import ApplicationService

TOOLCHAIN_DIGEST = digest(
    {
        "adapter": ADAPTER_VERSION,
        "cli": CLI_VERSION,
        "sdk": "0.163.0",
        "packager": "validated-source/v1",
    }
)


class DatabricksRuntimeFactory:
    """Only execution workers instantiate this credential-resolving factory."""

    def __init__(self, service: ApplicationService, settings: Settings):
        self.service, self.settings = service, settings
        self._clients: dict[tuple[UUID, UUID, int], tuple[float, WorkspaceClient]] = {}
        self._lock = threading.Lock()

    def _client(self, target):
        with self.service.database.transaction(target.tenant_id) as c:
            conn = Connection.model_validate(
                r.resource(c, target.tenant_id, "connection", target.connection_id)["data"]
            )
        binding = self.settings.credential_bindings.get(conn.credential_ref)
        if (
            not binding
            or binding.tenant_id != target.tenant_id
            or binding.host != target.host
            or conn.host != target.host
            or conn.revision != target.connection_revision
        ):
            raise Forbidden()
        key = (target.tenant_id, conn.id, conn.revision)
        with self._lock:
            cached = self._clients.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            options: dict[str, Any] = {
                "host": target.host,
                "http_timeout_seconds": 30,
                "retry_timeout_seconds": 30,
            }
            if binding.mode == "token_env":
                assert binding.token_env is not None
                options.update(token=os.environ[binding.token_env], auth_type="pat")
            elif binding.mode == "oauth_env":
                assert binding.client_id_env is not None and binding.client_secret_env is not None
                options.update(
                    client_id=os.environ[binding.client_id_env],
                    client_secret=os.environ[binding.client_secret_env],
                    auth_type="oauth-m2m",
                )
            else:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient

                value = (
                    SecretClient(
                        vault_url=binding.vault_url,
                        credential=DefaultAzureCredential(),
                        connection_timeout=5,
                        read_timeout=30,
                    )
                    .get_secret(binding.secret_name)
                    .value
                )
                if not value:
                    raise ValueError("configured credential is unavailable")
                options.update(token=value, auth_type="pat")
            client = WorkspaceClient(**options)
            self._clients[key] = (time.monotonic() + 60, client)
            return client

    def __call__(self, target):
        if target is None:
            return DatabricksAdapter(cli=DatabricksCLI(self.settings.databricks_cli))

        def token(host):
            if host != target.host:
                raise Forbidden()
            header = self._client(target).config.authenticate().get("Authorization", "")
            if not header.startswith("Bearer "):
                raise ValueError("bearer credential unavailable")
            return header.removeprefix("Bearer ")

        return DatabricksAdapter(
            cli=DatabricksCLI(self.settings.databricks_cli, token_provider=token),
            workspace_factory=lambda requested: DatabricksWorkspaceGateway(
                requested, client=self._client(requested)
            ),
        )
