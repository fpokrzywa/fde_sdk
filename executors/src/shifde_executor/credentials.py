"""Supervisor-only native credential resolution; handles are never public DTOs."""

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from pydantic import SecretStr
from shifde_contracts.common import utcnow
from shifde_contracts.jobs import CredentialGrant, ExecutionJob


@dataclass(frozen=True)
class CredentialHandle:
    bearer: SecretStr
    snowflake: dict[str, str] | None = None
    fabric: dict[str, Any] | None = None
    palantir: dict[str, Any] | None = None


class LocalCredentialBroker:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _handle(grant, bearer, binding):
        if grant.expires_at <= utcnow():
            raise PermissionError("credential grant expired during resolution")
        if not bearer:
            raise ValueError("credential is unavailable")
        profile = None
        if binding.platform == "snowflake":
            host = urlsplit(binding.host).hostname
            if not binding.snowflake_account or not binding.snowflake_role or not host:
                raise ValueError("Snowflake local profile requires account and role")
            profile = {
                "host": host,
                "account": binding.snowflake_account,
                "role": binding.snowflake_role,
            }
            if binding.snowflake_user:
                profile["user"] = binding.snowflake_user
        fabric = None
        if binding.platform == "fabric":
            if binding.host != "https://api.fabric.microsoft.com":
                raise ValueError("Fabric requires its canonical API origin")
            fabric = {
                "host": binding.host,
                "allow_iq": binding.allow_fabric_iq,
                "allow_notebook_execution": binding.allow_notebook_execution,
            }
        palantir = None
        if binding.platform == "palantir":
            palantir = {
                "host": binding.host,
                "allow_marketplace_preview": binding.allow_marketplace_preview,
                "approved_releases": [
                    r.model_dump(mode="json") for r in binding.palantir_approved_releases
                ],
            }
        return CredentialHandle(
            SecretStr(bearer), snowflake=profile, fabric=fabric, palantir=palantir
        )

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
        expected = (
            "palantir"
            if job.application_kind.startswith("palantir.")
            else "fabric"
            if job.application_kind in {"fabric.solution/v1", "fabric.iq/v1"}
            else "snowflake"
            if job.application_kind == "snowflake.native-app/v1"
            else "databricks"
        )
        if (
            binding.platform != expected
            or expected != "databricks"
            and binding.mode == "oauth_env"
            or expected != "fabric"
            and binding.mode.startswith("azure_")
            or expected != "palantir"
            and binding.mode == "palantir_client_credentials"
        ):
            raise PermissionError("credential platform/profile mismatch")
        if job.application_kind == "fabric.iq/v1" and not binding.allow_fabric_iq:
            raise PermissionError("Fabric IQ is disabled for this local credential binding")
        if binding.mode == "palantir_client_credentials":
            if (
                not binding.client_id_env
                or not binding.client_secret_env
                or not binding.palantir_scopes
            ):
                raise ValueError("Foundry OAuth requires explicit client and scope bindings")
            import httpx

            with httpx.Client(
                trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, connect=5)
            ) as client:
                with client.stream(
                    "POST",
                    binding.host + "/multipass/api/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": os.environ[binding.client_id_env],
                        "client_secret": os.environ[binding.client_secret_env],
                        "scope": " ".join(binding.palantir_scopes),
                    },
                ) as response:
                    if response.status_code != 200:
                        raise PermissionError("Foundry OAuth exchange failed")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise ValueError("Foundry OAuth response exceeded budget")
            import json

            value = json.loads(body)
            if (
                value.get("token_type", "").lower() != "bearer"
                or float(value.get("expires_in", 0)) <= 0
            ):
                raise PermissionError("Foundry OAuth token response invalid")
            return self._handle(grant, value["access_token"], binding)
        if binding.mode.startswith("azure_"):
            from azure.identity import (
                ClientSecretCredential,
                ManagedIdentityCredential,
                WorkloadIdentityCredential,
            )

            credential: Any
            if binding.mode == "azure_managed_identity":
                credential = (
                    ManagedIdentityCredential(client_id=str(binding.azure_client_id))
                    if binding.azure_client_id
                    else ManagedIdentityCredential()
                )
            else:
                if not binding.azure_tenant_id or not binding.azure_client_id:
                    raise ValueError("Azure authentication requires explicit tenant/client IDs")
                if binding.mode == "azure_client_secret":
                    if not binding.client_secret_env:
                        raise ValueError("Azure client secret environment binding is missing")
                    credential = ClientSecretCredential(
                        str(binding.azure_tenant_id),
                        str(binding.azure_client_id),
                        os.environ[binding.client_secret_env],
                    )
                else:
                    if not binding.azure_federated_token_file:
                        raise ValueError("Azure federated token file is missing")
                    credential = WorkloadIdentityCredential(
                        tenant_id=str(binding.azure_tenant_id),
                        client_id=str(binding.azure_client_id),
                        token_file_path=str(binding.azure_federated_token_file),
                    )
            with credential:
                token = credential.get_token("https://api.fabric.microsoft.com/.default")
            if token.expires_on <= utcnow().timestamp():
                raise PermissionError("Azure access token is expired")
            return self._handle(grant, token.token, binding)
        options: dict[str, Any] = {
            "host": binding.host,
            "http_timeout_seconds": 30,
            "retry_timeout_seconds": 10,
            "debug_headers": False,
        }
        if binding.mode == "token_file":
            if binding.token_file is None:
                raise ValueError("credential binding is incomplete")
            return self._handle(
                grant, binding.token_file.read_text(encoding="utf-8").strip(), binding
            )
        if binding.mode == "token_env":
            if not binding.token_env:
                raise ValueError("credential binding is incomplete")
            value = os.environ[binding.token_env]
            if not value:
                raise ValueError("credential is unavailable")
            return self._handle(grant, value, binding)
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
            return self._handle(grant, value, binding)
        # SDK handles OAuth expiry/refresh. A new resolution occurs at each header request.
        headers = WorkspaceClient(config=Config(**options)).config.authenticate()
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("native authentication is unavailable")
        return self._handle(grant, authorization.removeprefix("Bearer "), binding)
