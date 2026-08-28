"""A shipment that arrives without its custody log and consent registry must recover.

Boxes turn up without their paperwork; that is the ordinary case, not the exotic one. Before this
existed the checks went UNAVAILABLE, all twelve samples parked in WAITING_FOR_EVIDENCE, and nobody
was told what to send: the agent had no requirement to ask for, because a missing whole document
produced no requirement at all.
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
from biointake.domain.enums import CheckCategory, CheckStatus, RequirementType
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package

PI = headers("pi-kwame-osei")
TECH = headers("coordinator-ama-asante")
CASE, SHIP = "CASE-SHIP-PAPERLESS", "SHIP-PAPERLESS"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ok(response, label: str):
    assert response.status_code < 300, f"{label}: {response.status_code} {response.text[:400]}"
    return response.json()


@pytest.fixture
def bench():
    """A box on the bench, scanned and committed, whose paperwork was left behind."""
    client = TestClient(create_app(Settings(backend="memory", model_id="offline", users_spec=USERS_SPEC)))
    package = load_package(DEFAULT_FIXTURE_DIR)
    ok(
        client.post(
            "/api/shipments/announce",
            headers=PI,
            json={
                "case_id": CASE,
                "shipment_id": SHIP,
                "study_id": package.policy.protocol_id,
                "sender_site_id": package.shipment.sender_site_id,
                "announced_by_contact_id": "SITE-CONTACT-002",
                "manifest_csv_base64": b64(package.manifest_csv),
                "courier": "Arctic Courier",
                "tracking_reference": "AC-1",
                "container_count": 2,
                "logger_ids": ["LOGGER-A", "LOGGER-B"],
                "shipping_condition": "dry ice",
            },
        ),
        "announce",
    )
    ok(
        client.post(
            f"/api/cases/{CASE}/receipt",
            headers=TECH,
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
    ok(
        client.post(
            f"/api/cases/{CASE}/scan/bulk",
            headers=TECH,
            json={"text": "\n".join(f"BX-{n}" for n in range(201, 213))},
        ),
        "scan",
    )
    ok(client.post(f"/api/cases/{CASE}/batch/commit", headers=TECH, json={}), "commit")
    return client


def statuses(client, category: CheckCategory) -> Counter[str]:
    case = ok(client.get(f"/api/cases/{CASE}", headers=TECH), "case")
    return Counter(c["status"] for c in case["checks"] if c["category"] == category.value)


def test_the_missing_documents_are_asked_for_once_each(bench):
    ok(bench.post(f"/api/cases/{CASE}/run", headers=TECH, json={"event": "CASE_READY"}), "run")
    outbox = ok(bench.get(f"/api/cases/{CASE}/outbox", headers=TECH), "outbox")
    assert len(outbox) == 1, "one message, not one per sample"
    body = outbox[0]["body"]
    assert body.count("Chain-of-custody log for this shipment") == 1
    assert body.count("Consent registry for this shipment") == 1
    assert SHIP in body and "SHIP-DEMO-001" not in body  # the note names this shipment
    assert set(outbox[0]["affected_sample_ids"]) == {f"BX-{n}" for n in range(201, 213)}


def test_the_case_waits_for_evidence_rather_than_ending_in_a_warning(bench):
    run = ok(bench.post(f"/api/cases/{CASE}/run", headers=TECH, json={"event": "CASE_READY"}), "run")
    assert run["stable_state"] == "WAITING_FOR_EVIDENCE"
    assert run["warnings"] == []


def test_uploading_the_documents_re_decides_every_check_they_speak_to(bench):
    ok(bench.post(f"/api/cases/{CASE}/run", headers=TECH, json={"event": "CASE_READY"}), "run")
    before = statuses(bench, CheckCategory.CHAIN_OF_CUSTODY)
    assert before == Counter({CheckStatus.UNAVAILABLE.value: 12})

    request = ok(bench.get(f"/api/cases/{CASE}/outbox", headers=TECH), "outbox")[0]
    fixtures = Path(DEFAULT_FIXTURE_DIR)
    result = ok(
        bench.post(
            f"/api/evidence-requests/{request['request_id']}/complete",
            json={
                "upload_token": request["portal_path"].split("token=")[1],
                "submitted_by_contact_id": "SITE-CONTACT-002",
                "sender_message": "Both documents were left out of the box. Attached.",
                "files": [
                    {
                        "filename": "chain-of-custody.json",
                        "mime_type": "application/json",
                        "content_base64": b64((fixtures / "custody" / "chain-of-custody.json").read_bytes()),
                    },
                    {
                        "filename": "consent-records.json",
                        "mime_type": "application/json",
                        "content_base64": b64(
                            (fixtures / "consent" / "initial" / "consent-records.json").read_bytes()
                        ),
                    },
                ],
            },
        ),
        "complete",
    )
    assert result["checks_reverified"] > 0

    custody = statuses(bench, CheckCategory.CHAIN_OF_CUSTODY)
    assert custody[CheckStatus.PASS.value] == 11  # BX-207's identifier is still unconfirmed
    consent = statuses(bench, CheckCategory.CONSENT_VALIDITY)
    assert consent[CheckStatus.PASS.value] == 10  # two participants still need the addendum


def test_a_document_about_another_shipment_is_refused(bench):
    ok(bench.post(f"/api/cases/{CASE}/run", headers=TECH, json={"event": "CASE_READY"}), "run")
    request = ok(bench.get(f"/api/cases/{CASE}/outbox", headers=TECH), "outbox")[0]
    result = ok(
        bench.post(
            f"/api/evidence-requests/{request['request_id']}/complete",
            json={
                "upload_token": request["portal_path"].split("token=")[1],
                "submitted_by_contact_id": "SITE-CONTACT-002",
                "sender_message": "",
                "files": [
                    {
                        "filename": "chain-of-custody.json",
                        "mime_type": "application/json",
                        "content_base64": b64(
                            b'{"events": [{"sample_id": "ZZ-001", "event": "COLLECTED", '
                            b'"actor_id": "X", "timestamp": "2026-08-24T09:07:00+00:00"}]}'
                        ),
                    }
                ],
            },
        ),
        "complete",
    )
    assert result["checks_reverified"] == 0
    assert statuses(bench, CheckCategory.CHAIN_OF_CUSTODY) == Counter({CheckStatus.UNAVAILABLE.value: 12})


def test_a_shipment_document_is_one_requirement_not_one_per_sample(bench):
    ok(bench.post(f"/api/cases/{CASE}/run", headers=TECH, json={"event": "CASE_READY"}), "run")
    case = ok(bench.get(f"/api/cases/{CASE}", headers=TECH), "case")
    reqs = case["snapshot"]["unresolved_requirements"]
    shipment_wide = [r for r in reqs if not r["sample_id"]]
    assert {r["requirement_type"] for r in shipment_wide} == {
        RequirementType.CONSENT_REGISTRY.value,
        RequirementType.CUSTODY_LOG.value,
    }
    assert len(shipment_wide) == 2
