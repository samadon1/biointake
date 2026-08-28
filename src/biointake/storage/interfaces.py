"""Artifact byte storage. Local filesystem now; S3 with the same interface in Phase 4."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStorage(ABC):
    @abstractmethod
    def put(self, case_id: str, filename: str, data: bytes) -> tuple[str, str]:
        """Store bytes under a case-scoped key. Returns (storage_uri, sha256)."""

    @abstractmethod
    def get(self, storage_uri: str) -> bytes: ...

    @abstractmethod
    def exists(self, storage_uri: str) -> bool: ...
