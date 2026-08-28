"""S3 artifact storage. Keys are case-scoped: <prefix><case_id>/<sha12>-<safe filename>."""

from __future__ import annotations

import boto3

from .interfaces import ArtifactStorage, sha256_hex
from .local import sanitize_filename


class S3ArtifactStorage(ArtifactStorage):
    def __init__(
        self, bucket: str, prefix: str = "artifacts/", *, session: boto3.Session | None = None
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._s3 = (session or boto3.Session()).client("s3")

    def put(self, case_id: str, filename: str, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        key = f"{self._prefix}{sanitize_filename(case_id)}/{digest[:12]}-{sanitize_filename(filename)}"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}", digest

    def _key(self, storage_uri: str) -> str:
        prefix = f"s3://{self._bucket}/"
        if not storage_uri.startswith(prefix):
            raise ValueError(f"not a uri in this bucket: {storage_uri}")
        return storage_uri[len(prefix) :]

    def get(self, storage_uri: str) -> bytes:
        return bytes(self._s3.get_object(Bucket=self._bucket, Key=self._key(storage_uri))["Body"].read())

    def exists(self, storage_uri: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(storage_uri))
            return True
        except self._s3.exceptions.ClientError:
            return False

    def put_staged(self, case_id: str, event_id: str, filename: str, data: bytes) -> str:
        """Stage an upload for a specific invocation event; returns its uri."""
        key = f"{self._prefix}staged/{sanitize_filename(case_id)}/{sanitize_filename(event_id)}/{sanitize_filename(filename)}"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return f"s3://{self._bucket}/{key}"
