"""Content-verified local artifacts for a trusted operator, not a hosted tenant boundary."""

import hashlib
import io
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from uuid import UUID

from shifde_contracts.common import SourceArchive, relative_path

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FILES = 500


def read_archive(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds the size limit")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("expanded archive exceeds the limit")
        seen: set[str] = set()
        for info in entries:
            # ZipInfo normalizes backslashes on Windows. Validate the original name first.
            name = info.orig_filename.rstrip("/")
            relative_path(name)
            if name == ".":
                raise ValueError("archive entry must name a file or directory")
            if name.casefold() in seen:
                raise ValueError("duplicate archive entry")
            seen.add(name.casefold())
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("archive symlinks are forbidden")
            if info.is_dir():
                continue
            files[name] = archive.read(info)
    return files


def create_archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            relative_path(name)
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    result = output.getvalue()
    read_archive(result)
    return result


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LocalArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, reference: SourceArchive) -> Path:
        return self.root / str(reference.tenant_id) / f"{reference.id}.zip"

    def put(self, tenant_id: UUID, data: bytes) -> SourceArchive:
        read_archive(data)
        reference = SourceArchive(
            tenant_id=tenant_id, digest=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        atomic_write(self.path(reference), data)
        return reference

    def read(self, reference: SourceArchive, tenant_id: UUID) -> bytes:
        if reference.tenant_id != tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        data = self.path(reference).read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.digest or len(data) != reference.size:
            raise ValueError("artifact digest or size mismatch")
        read_archive(data)
        return data
