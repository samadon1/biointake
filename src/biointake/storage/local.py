from __future__ import annotations

import re
from pathlib import Path

from .interfaces import ArtifactStorage, sha256_hex

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    base = Path(name).name  # strip any directory components
    cleaned = _SAFE.sub("_", base).strip("._") or "artifact"
    return cleaned[:120]


class LocalArtifactStorage(ArtifactStorage):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, case_id: str, filename: str, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        safe = sanitize_filename(filename)
        target = self._root / sanitize_filename(case_id) / f"{digest[:12]}-{safe}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"local://{target.relative_to(self._root).as_posix()}", digest

    def _path(self, storage_uri: str) -> Path:
        if not storage_uri.startswith("local://"):
            raise ValueError(f"not a local uri: {storage_uri}")
        rel = Path(storage_uri.removeprefix("local://"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("path traversal rejected")
        return self._root / rel

    def get(self, storage_uri: str) -> bytes:
        return self._path(storage_uri).read_bytes()

    def exists(self, storage_uri: str) -> bool:
        return self._path(storage_uri).exists()


class MemoryArtifactStorage(ArtifactStorage):
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, case_id: str, filename: str, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        uri = f"mem://{sanitize_filename(case_id)}/{digest[:12]}-{sanitize_filename(filename)}"
        self._blobs[uri] = data
        return uri, digest

    def get(self, storage_uri: str) -> bytes:
        return self._blobs[storage_uri]

    def exists(self, storage_uri: str) -> bool:
        return storage_uri in self._blobs
