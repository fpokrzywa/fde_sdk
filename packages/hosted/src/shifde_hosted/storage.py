import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from shifde_contracts.common import SourceArchive
from shifde_core.artifact_store import MAX_ARCHIVE_BYTES, read_archive


class AzureArtifactStore:
    def __init__(self, container_url: str, cache_root: Path):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        parsed = urlsplit(container_url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("configure an HTTPS container URL without embedded credentials")
        self.root = cache_root.resolve()
        self.client = ContainerClient.from_container_url(
            container_url,
            credential=DefaultAzureCredential(),
            connection_timeout=5,
            read_timeout=30,
        )

    def put(self, tenant_id: UUID, data: bytes) -> SourceArchive:
        read_archive(data)
        reference = SourceArchive(
            tenant_id=tenant_id, digest=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        self.client.upload_blob(
            f"{tenant_id}/{reference.id}.zip", data, overwrite=False, timeout=30
        )
        return reference

    def read(self, reference: SourceArchive, tenant_id: UUID) -> bytes:
        if reference.tenant_id != tenant_id:
            raise PermissionError("artifact tenant mismatch")
        data = self.client.download_blob(
            f"{tenant_id}/{reference.id}.zip", offset=0, length=MAX_ARCHIVE_BYTES + 1, timeout=30
        ).readall()
        if len(data) != reference.size or hashlib.sha256(data).hexdigest() != reference.digest:
            raise ValueError("artifact digest mismatch")
        read_archive(data)
        return data
