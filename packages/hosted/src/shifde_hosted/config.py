from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shifde_contracts.common import Model, Target


class CredentialBinding(Model):
    tenant_id: UUID
    host: str
    require_approval: bool = False
    mode: Literal["token_env", "oauth_env", "key_vault", "private"]
    execution_pool_id: UUID | None = None
    token_env: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    vault_url: str | None = None
    secret_name: str | None = None
    _host = field_validator("host")(Target._validate_host)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "token_env" and not self.token_env:
            raise ValueError("token_env is required")
        if self.mode == "oauth_env" and not (self.client_id_env and self.client_secret_env):
            raise ValueError("OAuth identity environment names are required")
        if self.mode == "key_vault" and not (
            self.vault_url and self.vault_url.startswith("https://") and self.secret_name
        ):
            raise ValueError("an HTTPS vault and secret name are required")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHIFDE_", extra="ignore")
    database_url: SecretStr
    public_url: str = "https://delivery.example"
    auth_issuer: str
    auth_jwks_url: str | None = None
    auth_jwks_file: Path | None = None
    api_audience: str = "https://delivery.example"
    mcp_audience: str = "https://delivery.example/mcp"
    artifact_root: Path = Path(".shifde/hosted-artifacts")
    artifact_container_url: str | None = None
    credential_bindings: dict[str, CredentialBinding] = Field(default_factory=dict)
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = True
    temporal_tls_ca_file: Path | None = None
    temporal_tls_cert_file: Path | None = None
    temporal_tls_key_file: Path | None = None
    control_queue: str = "shifde-control"
    execution_queue: str = "shifde-execution"
    databricks_cli: str = "databricks"
    operation_timeout_seconds: int = Field(default=3600, ge=10, le=86400)
    poll_interval_seconds: float = Field(default=2, gt=0, le=60)
    require_approval_environments: list[str] = ["prod"]
    development: bool = False

    @model_validator(mode="after")
    def check_production(self):
        Target._validate_host(self.public_url)
        if not self.development and not self.temporal_tls:
            raise ValueError("production Temporal connections require TLS")
        if (
            not self.development
            and "sslmode=verify-full" not in self.database_url.get_secret_value()
        ):
            raise ValueError("production PostgreSQL connections require sslmode=verify-full")
        return self
