from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def relative_path(value: str) -> str:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("expected a relative POSIX source path")
    if value != "." and any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("source path cannot contain traversal or empty segments")
    if PurePosixPath(value).is_absolute():
        raise ValueError("source path must be relative")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
    }
    for part in value.split("/"):
        if part == "." and value == ".":
            continue
        if part.rstrip(" .") != part or part.split(".", 1)[0].upper() in reserved:
            raise ValueError("source path is ambiguous on Windows")
        if any(ord(char) < 32 or char in '<>"|?*' for char in part):
            raise ValueError("source path contains unsupported characters")
    return value


RelativePath = Annotated[str, AfterValidator(relative_path)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )


class Entity(Model):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID


class Target(Entity):
    connection_id: UUID
    connection_revision: int = Field(default=1, ge=1)
    environment: Identifier = "dev"
    host: str
    bindings: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def _validate_host(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError("host must be an HTTPS origin without credentials or a path")
        return value.rstrip("/")

    _host = field_validator("host")(_validate_host)


class SourceArchive(Entity):
    digest: Digest
    format: str = "zip"
    size: int = Field(ge=0)


class ResourceBinding(Model):
    key: str
    native_id: str
    resource_type: str
    application_id: UUID
    target_id: UUID


class Diagnostic(Model):
    code: str
    message: str
    path: str | None = None


class ValidationReport(Model):
    passed: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class Check(Model):
    name: str
    status: str = Field(pattern="^(passed|failed|skipped)$")
    required: bool = True
    evidence: str = ""


class VerificationReport(Model):
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return (
            bool(self.checks)
            and all(check.status == "passed" for check in self.checks if check.required)
            and any(check.required for check in self.checks)
        )


Timestamp = AwareDatetime
