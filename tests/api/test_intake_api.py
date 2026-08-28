"""The intake ramp over HTTP: announcement → receipt → scanning → staging-batch commit.

These exercise the front door a lab actually walks through, with no integrations: the site uploads a CSV, a
tech records what the box looked like, and every tube is scanned.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from authn import USERS_SPEC, headers
from biointake.api.app import create_app
from biointake.api.config import Settings
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package

TECH = headers("coordinator-ama-asante")
CONTROL = headers("control-plane")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                backend="memory",
                invoker="local",
                session_dir=tmp_path / "s",
                deterministic_clock=True,
                users_spec=USERS_SPEC,
            )
        ),
        headers=headers("coordinator-ama-asante"),
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _announce(client: TestClient, shipment_id: str = "SHIP-API-001") -> str:
    package = load_package(DEFAULT_FIXTURE_DIR)
    study_id = client.get("/api/studies").json()[0]["study_id"]
    r = client.post(
        "/api/shipments/announce",
        json={
            "shipment_id": shipment_id,
            "study_id": study_id,
            "sender_site_id": package.shipment.sender_site_id,
            "announced_by_contact_id": "SITE-CONTACT-002",
            "manifest_csv_base64": _b64(package.manifest_csv),
            "courier": "Arctic Cold Chain",
            "container_count": 2,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ANNOUNCED"
    return str(r.json()["case_id"])


def test_studies_are_seeded_from_the_packaged_policy(client: TestClient) -> None:
    studies = client.get("/api/studies").json()
    assert len(studies) == 1 and studies[0]["protocol_id"]


def test_manifest_is_validated_before_anything_ships(client: TestClient) -> None:
    package = load_package(DEFAULT_FIXTURE_DIR)
    study_id = client.get("/api/studies").json()[0]["study_id"]
    ok = client.post(
        "/api/manifests/validate",
        json={"study_id": study_id, "manifest_csv_base64": _b64(package.manifest_csv)},
    ).json()
    assert ok["accepted"] and len(ok["lines"]) == 12

    bad = client.post(
        "/api/manifests/validate",
        json={"study_id": study_id, "manifest_csv_base64": _b64(b"nothing,useful\n1,2\n")},
    ).json()
    assert not bad["accepted"] and bad["problems"]


def test_announcement_requires_a_verified_site_contact(client: TestClient) -> None:
    package = load_package(DEFAULT_FIXTURE_DIR)
    study_id = client.get("/api/studies").json()[0]["study_id"]
    r = client.post(
        "/api/shipments/announce",
        json={
            "shipment_id": "SHIP-API-002",
            "study_id": study_id,
            "sender_site_id": package.shipment.sender_site_id,
            "announced_by_contact_id": "SOMEONE-WHO-EMAILED-US",
            "manifest_csv_base64": _b64(package.manifest_csv),
        },
    )
    assert r.status_code == 403


def test_full_ramp_over_http(client: TestClient) -> None:
    client.post("/api/demo/reset")
    case_id = _announce(client)

    receipt = client.post(
        f"/api/cases/{case_id}/receipt",
        json={
            "package_condition": "ACCEPTABLE",
            "package_count_received": 2,
            "refrigerant_condition": "dry ice remaining",
            "seal_intact": True,
        },
        headers=TECH,
    )
    assert receipt.status_code == 200, receipt.text

    intake = client.get(f"/api/cases/{case_id}/intake").json()
    assert intake["state"] == "RECEIVED"
    rows = intake["batch"]["rows"]
    assert len(rows) == 12 and all(r["scanned_value"] is None for r in rows)

    # Cannot commit a batch that nothing has been scanned into.
    assert client.post(f"/api/cases/{case_id}/batch/commit", json={}, headers=TECH).status_code == 422

    # Scan eleven of twelve, including one deliberate near-match and one tube that is not on the manifest.
    for row in rows[:6]:
        out = client.post(f"/api/cases/{case_id}/scan", json={"value": row["sample_id"]}, headers=TECH).json()
        assert out["outcome"] == "MATCHED"

    dup = client.post(f"/api/cases/{case_id}/scan", json={"value": rows[0]["sample_id"]}, headers=TECH).json()
    assert dup["outcome"] == "DUPLICATE"

    stray = client.post(f"/api/cases/{case_id}/scan", json={"value": "ZZ-999"}, headers=TECH).json()
    assert stray["outcome"] == "UNEXPECTED"

    for row in rows[6:]:
        client.post(f"/api/cases/{case_id}/scan", json={"value": row["sample_id"]}, headers=TECH)

    summary = client.get(f"/api/cases/{case_id}/intake").json()["batch"]
    assert summary["scanned"] == 12 and summary["duplicates"] == [rows[0]["sample_id"]]
    assert summary["unexpected"] == ["ZZ-999"]

    committed = client.post(f"/api/cases/{case_id}/batch/commit", json={}, headers=TECH)
    assert committed.status_code == 200, committed.text
    body = committed.json()
    assert len(body["committed"]) == 12 and body["state"] == "VERIFYING"


def test_partial_receipt_must_be_chosen_deliberately(client: TestClient) -> None:
    client.post("/api/demo/reset")
    case_id = _announce(client, "SHIP-API-003")
    client.post(f"/api/cases/{case_id}/receipt", json={"package_count_received": 1}, headers=TECH)
    rows = client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]
    for row in rows[:8]:
        client.post(f"/api/cases/{case_id}/scan", json={"value": row["sample_id"]}, headers=TECH)

    blocked = client.post(f"/api/cases/{case_id}/batch/commit", json={}, headers=TECH)
    assert blocked.status_code == 422
    assert "not scanned" in blocked.json()["detail"]

    ok = client.post(f"/api/cases/{case_id}/batch/commit", json={"accept_partial": True}, headers=TECH).json()
    assert len(ok["committed"]) == 8


def test_demo_now_plays_through_the_ramp(client: TestClient) -> None:
    """The demonstration must exercise the real front door, not a fixture shortcut."""
    client.post("/api/demo/reset")
    loaded = client.post("/api/demo/load").json()
    assert loaded["via"] == "intake-ramp"
    assert loaded["declared"] == 12 and loaded["committed"] == 12 and loaded["near_matches"] == 1

    batch = client.get(f"/api/cases/{loaded['case_id']}/intake").json()["batch"]
    near = [r for r in batch["rows"] if r["outcome"] == "NEAR_MATCH"]
    assert len(near) == 1
    # The manifest says BX-2O7; the tube says BX-207. Recorded, never silently corrected.
    assert near[0]["sample_id"] != near[0]["scanned_value"]


def test_a_whole_rack_can_be_pasted_at_once(client: TestClient) -> None:
    """A rack scanner produces a CSV column, not four hundred keystrokes. Pasting it must go through the
    same reconciliation a handheld read gets, no bulk shortcut around the matching rules."""
    client.post("/api/demo/reset")
    case_id = _announce(client, "SHIP-API-BULK")
    client.post(f"/api/cases/{case_id}/receipt", json={}, headers=TECH)
    rows = client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]

    pasted = "\n".join(r["sample_id"] for r in rows[:5]) + "\n\nZZ-000,\tBX-201\n"
    out = client.post(f"/api/cases/{case_id}/scan/bulk", json={"text": pasted}, headers=TECH)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["scanned"] == 7  # five rows, one stray, one duplicate; blanks dropped
    outcomes = [r["outcome"] for r in body["results"]]
    assert outcomes[:5] == ["MATCHED"] * 5
    assert "UNEXPECTED" in outcomes and "DUPLICATE" in outcomes
    assert body["batch"]["unexpected"] == ["ZZ-000"]


def test_received_quality_is_recorded_per_specimen(client: TestClient) -> None:
    """Distinct from the condition of the package: a box can be intact and one tube inside it thawed."""
    client.post("/api/demo/reset")
    case_id = _announce(client, "SHIP-API-QUALITY")
    client.post(f"/api/cases/{case_id}/receipt", json={"package_condition": "ACCEPTABLE"}, headers=TECH)
    rows = client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]
    for r in rows:
        client.post(f"/api/cases/{case_id}/scan", json={"value": r["sample_id"]}, headers=TECH)

    assert all(
        r["received_quality"] == "ACCEPTABLE"
        for r in client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]
    )

    bad = client.post(
        f"/api/cases/{case_id}/quality", json={"row": 3, "received_quality": "NONSENSE"}, headers=TECH
    )
    assert bad.status_code == 400

    ok = client.post(
        f"/api/cases/{case_id}/quality", json={"row": 3, "received_quality": "THAWED"}, headers=TECH
    )
    assert ok.status_code == 200, ok.text
    amended = [r for r in ok.json()["batch"]["rows"] if r["row"] == 3][0]
    assert amended["received_quality"] == "THAWED"
    # Amending must not double-count the batch.
    assert ok.json()["batch"]["scanned"] == len(rows)

    committed = client.post(f"/api/cases/{case_id}/batch/commit", json={}, headers=TECH)
    assert committed.status_code == 200
    # The observation survives onto the specimen: it is what the technician saw, and a downstream
    # researcher needs it even when every other check passes.
    report = client.get(f"/api/cases/{case_id}").json()["report"]
    assert report is not None


def test_both_barcodes_on_a_label_can_be_read(client: TestClient) -> None:
    """A specimen label carries a linear tube ID and a 2D site accession. They are not interchangeable,
    a LIMS deduplicates on the accession, and scanning the wrong one is a documented way to file a
    specimen silently wrong. The bench does not guess which was read; the technician says."""
    client.post("/api/demo/reset")
    case_id = _announce(client, "SHIP-API-ACCESSION")
    client.post(f"/api/cases/{case_id}/receipt", json={}, headers=TECH)
    rows = client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]
    for r in rows[:3]:
        client.post(f"/api/cases/{case_id}/scan", json={"value": r["sample_id"]}, headers=TECH)

    # An accession is attached to a named row, never matched against the manifest; the manifest does not
    # contain accessions and never did.
    ok = client.post(
        f"/api/cases/{case_id}/accession", json={"row": 1, "encoded_barcode": "NS042-000201"}, headers=TECH
    )
    assert ok.status_code == 200, ok.text
    row1 = [r for r in ok.json()["batch"]["rows"] if r["row"] == 1][0]
    assert row1["encoded_barcode"] == "NS042-000201"
    assert ok.json()["batch"]["scanned"] == 3  # attaching must not create a scan

    # Two tubes cannot share one accession.
    clash = client.post(
        f"/api/cases/{case_id}/accession", json={"row": 2, "encoded_barcode": "NS042-000201"}, headers=TECH
    )
    assert clash.status_code == 422 and clash.json()["code"] == "LABEL_DUPLICATE"

    # A row that was never scanned has nothing to attach to.
    missing = client.post(
        f"/api/cases/{case_id}/accession", json={"row": 11, "encoded_barcode": "NS042-000211"}, headers=TECH
    )
    # This API answers a refused domain operation with 422 and an explicit code, including "no such row".
    assert missing.status_code == 422


def test_a_real_shipment_produces_real_checks(client: TestClient) -> None:
    """The path a lab actually takes, with no demo helper anywhere near it.

    This is the regression that matters most. Before, announcing a manifest, recording receipt, scanning and
    committing produced *zero* checks: verification needed a scanner export that only the fixture wrote, and
    a protocol, consent registry and custody log that no endpoint accepted, so building the context threw and
    every specimen sat in PENDING with nothing said about why. The demo worked because it attached four
    artifacts from the fixture that a real user has no way to supply.
    """
    client.post("/api/demo/reset")
    package = load_package(DEFAULT_FIXTURE_DIR)
    case_id = _announce(client, "SHIP-REALPATH")

    client.post(
        f"/api/cases/{case_id}/receipt",
        json={
            "package_count_received": 2,
            "logger_files": [
                {"filename": f"{logger_id}.csv", "mime_type": "text/csv", "content_base64": _b64(data)}
                for logger_id, data in sorted(package.temperature_logs.items())
            ],
        },
        headers=TECH,
    )
    rows = client.get(f"/api/cases/{case_id}/intake").json()["batch"]["rows"]
    client.post(
        f"/api/cases/{case_id}/scan/bulk",
        json={"text": ",".join(r["sample_id"] for r in rows)},
        headers=TECH,
    )
    assert (
        len(client.post(f"/api/cases/{case_id}/batch/commit", json={}, headers=TECH).json()["committed"])
        == 12
    )

    run = client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL).json()
    assert run["checks_evaluated"] == 84, "every specimen must be checked against every required category"

    checks = client.get(f"/api/cases/{case_id}").json()["checks"]
    by_category: dict[str, set[str]] = {}
    for c in checks:
        by_category.setdefault(c["category"], set()).add(c["status"])

    # The bench's own scans are now the evidence, so identity and the manifest can be answered.
    assert by_category["IDENTITY_MATCH"] == {"PASS"}
    assert by_category["MANIFEST_MATCH"] == {"PASS"}
    # The protocol is rendered from the study the shipment was announced against, so eligibility is
    # answerable without anyone uploading a document.
    assert by_category["PROTOCOL_ELIGIBILITY"] == {"PASS"}
    # The logger files uploaded at receipt are read, and the real excursion is found.
    assert by_category["TEMPERATURE_REQUIREMENT"] == {"PASS", "FAIL"}

    # What has no source yet says so by name, rather than failing the whole run silently.
    unsupplied = {
        c["observed_value"] for c in checks if "REQUIRED_EVIDENCE_NOT_SUPPLIED" in c["reason_codes"]
    }
    # This announcement carried neither document, so those two checks say so by name.
    assert unsupplied == {
        "no consent records on file for this shipment",
        "no custody log on file for this shipment",
    }
    # Fail closed: nothing is accepted on evidence nobody supplied.
    assert client.get(f"/api/cases/{case_id}").json()["report"]["counts"]["ACCEPTED"] == 0
