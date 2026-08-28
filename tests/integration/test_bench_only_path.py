"""Everything the demo button does is reachable from the receiving bench.

The console has a "Load demonstration" control that stages a shipment in one click. It is a
convenience, not the product, and a judge, or a lab, must be able to reach the same place by
doing the work. This drives the whole case through the ordinary endpoints and asserts it lands
exactly where the demo loader lands.

It failed before the lab's record system moved out of the demo loader: a shipment announced by
hand was reconciled against an empty LIMS, so LIMS_RECONCILIATION was UNAVAILABLE on all twelve,
nothing could be accepted, and the accession collision the lab exists to catch was not there to
catch.
"""

from __future__ import annotations

import base64
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from authn import USERS_SPEC, headers
from biointake.api.app import create_app
from biointake.api.config import Settings
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.services.manifest import parse_scanner_export

CASE, SHIP = "CASE-SHIP-BENCH", "SHIP-BENCH"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ok(response, label: str):
    assert response.status_code < 300, f"{label}: {response.status_code} {response.text[:300]}"
    return response.json()


@pytest.fixture
def bench(tmp_path: Path):
    """A coordinator signed in to a lab that has never had the demo loaded."""
    app = create_app(
        Settings(
            backend="memory",
            invoker="local",
            model_id="offline",
            session_dir=tmp_path / "sessions",
            users_spec=USERS_SPEC,
        )
    )
    return TestClient(app, headers=headers("coordinator-ama-asante"))


def walk(client: TestClient) -> dict:
    package = load_package(DEFAULT_FIXTURE_DIR)
    ok(
        client.post(
            "/api/shipments/announce",
            json={
                "case_id": CASE,
                "shipment_id": SHIP,
                "study_id": package.policy.protocol_id,
                "sender_site_id": package.shipment.sender_site_id,
                "announced_by_contact_id": "SITE-CONTACT-002",
                "manifest_csv_base64": b64(package.manifest_csv),
                "courier": "Arctic Cold Chain",
                "tracking_reference": "ACC-2026-08-0412",
                "container_count": 2,
                "logger_ids": ["LOGGER-A", "LOGGER-B"],
                "shipping_condition": "dry ice",
                "custody_log_base64": b64(package.custody_log_json),
                "consent_records_base64": b64(package.consent_records_json),
            },
        ),
        "announce",
    )
    ok(
        client.post(
            f"/api/cases/{CASE}/receipt",
            json={
                "package_count_received": 2,
                "condition": "INTACT",
                "logger_files": [
                    {"filename": f"{k}.csv", "mime_type": "text/csv", "content_base64": b64(v)}
                    for k, v in package.temperature_logs.items()
                ],
            },
        ),
        "receipt",
    )
    for scan in parse_scanner_export(package.scanner_export_json).scans:
        ok(
            client.post(
                f"/api/cases/{CASE}/scan",
                json={
                    "value": scan.sample_id,
                    "container_id": scan.container_id,
                    "encoded_barcode": scan.barcode,
                },
            ),
            f"scan {scan.sample_id}",
        )
    ok(client.post(f"/api/cases/{CASE}/batch/commit", json={}), "commit")
    return ok(client.post(f"/api/cases/{CASE}/run", json={"event_type": "CASE_READY"}), "run")


def test_the_bench_reaches_the_same_place_as_the_demo_button(bench):
    run = walk(bench)
    assert run["stable_state"] == "WAITING_FOR_EVIDENCE"
    assert run["warnings"] == []
    case = ok(bench.get(f"/api/cases/{CASE}"), "case")
    assert Counter(s["state"] for s in case["snapshot"]["samples"]) == Counter(
        {"ACCEPTED": 7, "WAITING_FOR_EVIDENCE": 3, "QUARANTINED": 1, "NEEDS_HUMAN_DECISION": 1}
    )


def test_a_hand_announced_shipment_reconciles_against_the_labs_own_records(bench):
    """The LIMS belongs to the lab, not to the demo. Without it nothing can be accepted at all."""
    walk(bench)
    case = ok(bench.get(f"/api/cases/{CASE}"), "case")
    lims = Counter(c["status"] for c in case["checks"] if c["category"] == "LIMS_RECONCILIATION")
    assert lims["PASS"] == 11
    assert lims["FAIL"] == 1  # BX-211's accession belongs to an archived record
    assert lims["UNAVAILABLE"] == 0


def test_the_collision_the_lab_exists_to_catch_is_caught(bench):
    walk(bench)
    case = ok(bench.get(f"/api/cases/{CASE}"), "case")
    quarantined = [s for s in case["snapshot"]["samples"] if s["state"] == "QUARANTINED"]
    assert [s["sample_id"] for s in quarantined] == ["BX-211"]


def test_no_demo_endpoint_was_touched(bench):
    """Guards the point of the test: the demo loader must not have run behind our backs."""
    walk(bench)
    cases = ok(bench.get("/api/cases"), "cases")
    assert [c["shipment_id"] for c in cases] == [SHIP]
