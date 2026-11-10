"""Supervisor-only native credential resolution; handles are never public DTOs."""

import os
from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from pydantic import SecretStr
from shifde_contracts.common import utcnow
from shifde_contracts.jobs import CredentialGrant, ExecutionJob


@dataclass(frozen=True)
class CredentialHandle:
    bearer: SecretStr


class LocalCredentialBroker:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _handle(grant, bearer):
        if grant.expires_at <= utcnow():
            raise PermissionError("credential grant expired during resolution")
        if not bearer:
            raise ValueError("credential is unavailable")
        return CredentialHandle(SecretStr(bearer))

    def resolve(self, grant: CredentialGrant, job: ExecutionJob) -> CredentialHandle:
        if (
            grant.expires_at <= utcnow()
            or grant.tenant_id != self.settings.tenant_id
            or grant.pool_id != self.settings.pool_id
            or grant.worker_id != self.settings.worker_id
            or grant.job_id != job.id
            or not job.target
            or grant.target_id != job.target.id
            or grant.audience != job.target.host
            or grant.credential_ref != job.credential_ref
        ):
            raise PermissionError("credential grant does not match this execution")
        binding = self.settings.credential_bindings.get(grant.credential_ref)
        if (
            binding is None
            or binding.tenant_id != grant.tenant_id
            or binding.host != grant.audience
        ):
            raise PermissionError("local credential binding is unavailable")
        options: dict[str, Any] = {
            "host": binding.host,
            "http_timeout_seconds": 30,
            "retry_timeout_seconds": 10,
            "debug_headers": False,
        }
        if binding.mode == "token_env":
            if not binding.token_env:
                raise ValueError("credential binding is incomplete")
            value = os.environ[binding.token_env]
            if not value:
                raise ValueError("credential is unavailable")
            return self._handle(grant, value)
        elif binding.mode == "oauth_env":
            if not binding.client_id_env or not binding.client_secret_env:
                raise ValueError("credential binding is incomplete")
            options.update(
                client_id=os.environ[binding.client_id_env],
                client_secret=os.environ[binding.client_secret_env],
                auth_type="oauth-m2m",
            )
        else:
            if (
                not binding.vault_url
                or not binding.vault_url.startswith("https://")
                or not binding.secret_name
            ):
                raise ValueError("credential binding is incomplete")
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            with SecretClient(
                vault_url=binding.vault_url,
                credential=DefaultAzureCredential(),
                connection_timeout=5,
                read_timeout=30,
            ) as vault:
                value = vault.get_secret(binding.secret_name).value
            if not value:
                raise ValueError("credential is unavailable")
            return self._handle(grant, value)
        # SDK handles OAuth expiry/refresh. A new resolution occurs at each header request.
        headers = WorkspaceClient(config=Config(**options)).config.authenticate()
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("native authentication is unavailable")
        return self._handle(grant, authorization.removeprefix("Bearer "))
