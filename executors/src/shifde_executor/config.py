import sys
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shifde_contracts.common import Model, Target


class LocalCredentialBinding(Model):
    tenant_id: UUID
    host: str
    platform: Literal["databricks", "snowflake", "fabric"] = "databricks"
    mode: Literal[
        "token_env",
        "token_file",
        "oauth_env",
        "key_vault",
        "azure_client_secret",
        "azure_managed_identity",
        "azure_workload_identity",
    ]
    token_file: Path | None = None
    snowflake_account: str | None = None
    snowflake_role: str | None = None
    snowflake_user: str | None = None
    token_env: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    azure_tenant_id: UUID | None = None
    azure_client_id: UUID | None = None
    azure_federated_token_file: Path | None = None
    allow_fabric_iq: bool = False
    allow_notebook_execution: bool = False
    vault_url: str | None = None
    secret_name: str | None = None
    _host = field_validator("host")(Target._validate_host)


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHIFDE_EXECUTOR_", extra="forbid")
    gateway_url: str
    token_file: Path
    tenant_id: UUID
    pool_id: UUID
    worker_id: UUID
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}$")
    credential_bindings: dict[str, LocalCredentialBinding] = Field(default_factory=dict)
    isolation: Literal["dedicated_vm", "development"] = "dedicated_vm"
    dedicated_tenant_id: UUID | None = None
    cosign_key: Path | None = None
    native_network: str = Field(default="none", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    docker: str = "docker"
    cosign: str = "cosign"
    poll_seconds: float = Field(default=2, gt=0, le=30)
    heartbeat_seconds: float = Field(default=10, gt=0, le=15)
    max_job_seconds: int = Field(default=3600, ge=1, le=86400)
    state_dir: Path = Path(".shifde/executor-receipts")

    _gateway = field_validator("gateway_url")(Target._validate_host)

    @model_validator(mode="after")
    def security_boundary(self):
        if self.isolation == "dedicated_vm" and sys.platform != "linux":
            raise ValueError("production executor hosts must run Linux")
        if self.isolation == "dedicated_vm" and (
            self.dedicated_tenant_id != self.tenant_id or self.cosign_key is None
        ):
            raise ValueError(
                "production requires a dedicated tenant VM and a trusted image-signing key"
            )
        if self.native_network in {"host", "bridge", "container"}:
            raise ValueError("native egress requires an operator-provisioned restricted network")
        return self
