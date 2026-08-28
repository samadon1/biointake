"""AWS-backed repository/storage tests. Skipped unless BIOINTAKE_AWS_TESTS=1 (uses the real table/bucket)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BIOINTAKE_AWS_TESTS") != "1",
    reason="set BIOINTAKE_AWS_TESTS=1 with a configured profile/table/bucket",
)


@pytest.fixture(scope="module")
def aws():  # type: ignore[no-untyped-def]
    import boto3

    from biointake.repositories.dynamodb import DynamoDBRepository
    from biointake.storage.s3 import S3ArtifactStorage

    session = boto3.Session(
        profile_name=os.environ.get("BIOINTAKE_AWS_PROFILE"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    return DynamoDBRepository(os.environ["BIOINTAKE_DDB_TABLE"], session=session), S3ArtifactStorage(
        os.environ["BIOINTAKE_S3_BUCKET"], session=session
    )


def test_case_roundtrip_and_listing(aws):  # type: ignore[no-untyped-def]
    from biointake.domain.models import Sample, ShipmentCase

    repo, _ = aws
    now = datetime.now(UTC)
    case_id = f"CASE-T-{uuid.uuid4().hex[:8]}"
    case = ShipmentCase(
        case_id=case_id,
        shipment_id="S",
        protocol_id="P",
        protocol_version="1",
        sender_site_id="site",
        received_at=now,
        agent_session_id=str(uuid.uuid4()),
        expected_sample_count=1,
        created_at=now,
        updated_at=now,
    )
    repo.save_case(case)
    repo.save_sample(
        Sample(
            sample_id=f"{case_id}-BX-1",
            case_id=case_id,
            barcode="b",
            specimen_type="PLASMA",
            container_id="BOX",
            expected_protocol_id="P",
            updated_at=now,
        )
    )
    assert repo.get_case(case_id) == case
    assert [s.sample_id for s in repo.list_samples(case_id)] == [f"{case_id}-BX-1"]
    ids = repo.next_ids("T", 3)
    assert len(ids) == 3 and ids[0] != ids[2]
    assert repo.acquire_lease(case_id, "A", 60) and not repo.acquire_lease(case_id, "B", 60)
    repo.release_lease(case_id, "A")
    assert repo.acquire_lease(case_id, "B", 60)
    assert repo.purge_case(case_id) >= 2


def test_s3_storage_roundtrip(aws):  # type: ignore[no-untyped-def]
    _, storage = aws
    uri, digest = storage.put("CASE-T", "hello.txt", b"hello")
    assert storage.exists(uri) and storage.get(uri) == b"hello" and len(digest) == 64
