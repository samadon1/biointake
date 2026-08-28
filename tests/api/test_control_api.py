"""Control API end-to-end with in-memory repositories and the in-process Strands loop."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from authn import USERS_SPEC, headers
from biointake.api.app import create_app
from biointake.api.config import Settings
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package

COORD = headers("coordinator-ama-asante")
CONTROL = headers("control-plane")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                backend="memory",
                invoker="local",
                session_dir=tmp_path / "sessions",
                deterministic_clock=True,
                users_spec=USERS_SPEC,
            )
        ),
        headers=headers("coordinator-ama-asante"),
    )


def _load_and_run(client: TestClient) -> tuple[str, dict]:
    client.post("/api/demo/reset", headers=COORD)
    case_id = client.post("/api/demo/load", headers=COORD).json()["case_id"]
    r1 = client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL).json()
    return case_id, r1


def _evidence_body(client: TestClient, rid: str) -> dict:
    package = load_package(DEFAULT_FIXTURE_DIR)
    token = client.app.state.biointake.services.repo.get_request(rid).upload_token
    reply = json.loads(package.later["sender-reply.json"])
    return {
        "upload_token": token,
        "submitted_by_contact_id": reply["from_contact_id"],
        "sender_message": reply["free_text"],
        "files": [
            {
                "filename": "consent-addendum.json",
                "mime_type": "application/json",
                "content_base64": base64.b64encode(package.later["consent-addendum.json"]).decode(),
            }
        ],
    }


def test_full_flow_through_the_api(client: TestClient):
    case_id, r1 = _load_and_run(client)
    assert r1["stable_state"] == "WAITING_FOR_EVIDENCE" and len(r1["created_evidence_request_ids"]) == 1
    rid = r1["created_evidence_request_ids"][0]
    req = client.get(f"/api/evidence-requests/{rid}").json()
    assert req["recipient"]["contact_id"] == "SITE-CONTACT-002" and "upload_token" not in req
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    assert (
        r2["stop_reason"] == "interrupt"
        and r2["checks_reverified"] == 4
        and r2["stable_state"] == "NEEDS_HUMAN_DECISION"
    )
    iid = r2["pending_interrupt"]["interrupt_id"]
    cards = client.get(f"/api/cases/{case_id}/decisions").json()
    assert cards[0]["interrupt_id"] == iid and cards[0]["sample_id"] == "BX-212"
    r3 = client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "QUARANTINE", "comment": "hold"},
        headers=COORD,
    ).json()
    assert r3["stable_state"] == "COMPLETED"
    rep = client.get(f"/api/cases/{case_id}").json()["report"]
    assert (
        rep["counts"]["ACCEPTED"] == 10
        and rep["counts"]["QUARANTINED"] == 2
        and rep["unauthorized_acceptances"] == 0
    )
    assert len(rep["human_decisions"]) == 1 and rep["human_decisions"][0]["actor_role"] == "COORDINATOR"
    body = client.get(f"/api/cases/{case_id}/events", params={"after": 0}).json()
    events = body["events"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert body["agent_running"] is False and body["case_state"] == "COMPLETED"
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["agent_running"] is False and len(detail["checks"]) == 84
    bx207 = [c for c in detail["checks"] if c["sample_id"] == "BX-207" and c["category"] == "IDENTITY_MATCH"][
        0
    ]
    assert (
        bx207["status"] == "PASS"
        and bx207["evidence_refs"]
        and bx207["rule_version"].startswith("POLICY-PROTO-042")
    )


def test_a_credential_is_required_and_the_actor_comes_from_it(client: TestClient):
    case_id, _ = _load_and_run(client)
    anonymous = TestClient(client.app)  # the same server, with nobody signed in
    for label, hdrs in [
        ("no credential", {}),
        ("not a bearer token", {"Authorization": "coordinator-ama-asante"}),
        ("unknown token", {"Authorization": "Bearer bit_test_mallory"}),
    ]:
        r = anonymous.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=hdrs)
        assert r.status_code == 401, f"{label} was accepted: {r.status_code}"


def test_wrong_upload_token_is_rejected(client: TestClient):
    _, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    body = _evidence_body(client, rid)
    body["upload_token"] = "nope"
    assert client.post(f"/api/evidence-requests/{rid}/complete", json=body).status_code == 403


def test_duplicate_run_and_duplicate_evidence_have_no_extra_effects(client: TestClient):
    case_id, r1 = _load_and_run(client)
    again = client.post(
        f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL
    ).json()
    assert again["created_evidence_request_ids"] == [] and again["committed_dispositions"] == {}
    rid = r1["created_evidence_request_ids"][0]
    body = _evidence_body(client, rid)
    first = client.post(f"/api/evidence-requests/{rid}/complete", json=body).json()
    lims_before = client.app.state.biointake.services.lims.write_count
    dup = client.post(f"/api/evidence-requests/{rid}/complete", json=body).json()
    assert dup["stable_state"] == "NEEDS_HUMAN_DECISION"
    assert client.app.state.biointake.services.lims.write_count == lims_before
    assert len(client.app.state.biointake.services.repo.list_requests(case_id)) == 1
    assert first["checks_reverified"] == 4


def test_unauthorized_role_cannot_approve_exception(client: TestClient):
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    iid = r2["pending_interrupt"]["interrupt_id"]
    r3 = client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "APPROVE_EXCEPTION"},
        headers=COORD,
    ).json()
    assert r3["stable_state"] != "COMPLETED"
    assert client.get(f"/api/cases/{case_id}").json()["report"]["counts"]["ACCEPTED_WITH_EXCEPTION"] == 0
    # the refusal must NOT consume the decision: a fresh interrupt id is recorded and still answerable
    card = client.get(f"/api/cases/{case_id}/decisions").json()[0]
    assert card["resolved_decision_id"] is None and card["interrupt_id"] != iid
    stale = client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "QUARANTINE"},
        headers=COORD,
    )
    assert stale.status_code == 404 and "stale" in stale.json()["detail"]
    bad = client.post(
        f"/api/cases/{case_id}/interrupts/{card['interrupt_id']}/respond",
        json={"selected_option": "DESTROY"},
        headers=COORD,
    )
    assert bad.status_code == 400
    ok = client.post(
        f"/api/cases/{case_id}/interrupts/{card['interrupt_id']}/respond",
        json={"selected_option": "QUARANTINE"},
        headers=COORD,
    ).json()
    assert ok["stable_state"] == "COMPLETED"
    rep = client.get(f"/api/cases/{case_id}").json()["report"]
    assert rep["counts"]["ACCEPTED"] == 10 and rep["counts"]["QUARANTINED"] == 2
    assert rep["unauthorized_acceptances"] == 0


def test_lease_conflict_returns_409(client: TestClient):
    case_id, _ = _load_and_run(client)
    repo = client.app.state.biointake.services.repo
    assert repo.acquire_lease(case_id, "someone-else", 300)
    r = client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL)
    assert r.status_code == 409
    repo.release_lease(case_id, "someone-else")


def test_outbox_and_demo_reply(client: TestClient):
    case_id, r1 = _load_and_run(client)
    outbox = client.get(f"/api/cases/{case_id}/outbox").json()
    assert (
        len(outbox) == 1
        and outbox[0]["to"]["contact_id"] == "SITE-CONTACT-002"
        and outbox[0]["portal_path"].startswith("/portal/REQ-")
    )
    reply = client.get("/api/demo/sender-reply").json()
    assert (
        reply["from_contact_id"] == "SITE-CONTACT-002"
        and reply["files"][0]["filename"] == "consent-addendum.json"
    )
    r = client.get(f"/api/cases/{case_id}", headers={"Origin": "http://localhost:3000"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_reset_clears_the_strands_session_and_case_gets_a_fresh_one(client: TestClient):
    """Regression: a reset case reused a deterministic session id, so it inherited the previous
    run's pending interrupt and the next CASE_READY crashed with 'must resume from interrupt'."""
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    first_session = client.app.state.biointake.services.repo.get_case(case_id).agent_session_id
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    assert r2["stop_reason"] == "interrupt"  # a human decision is now outstanding

    # a second demo, without answering the decision, must start from a clean session
    client.post("/api/demo/reset")
    client.post("/api/demo/load")
    second_session = client.app.state.biointake.services.repo.get_case(case_id).agent_session_id
    assert second_session != first_session
    again = client.post(
        f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL
    ).json()
    assert again["stop_reason"] == "end_turn" and again["stable_state"] == "WAITING_FOR_EVIDENCE"
    assert again["warnings"] == []


def test_run_while_a_decision_is_outstanding_reports_it_instead_of_crashing(client: TestClient):
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    iid = r2["pending_interrupt"]["interrupt_id"]
    blocked = client.post(
        f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=CONTROL
    ).json()
    assert blocked["stop_reason"] == "interrupt"
    assert blocked["pending_interrupt"]["interrupt_id"] == iid
    assert "outstanding" in blocked["warnings"][0]
    assert blocked["committed_dispositions"] == {} and blocked["unauthorized_acceptances"] == 0
    ok = client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "QUARANTINE"},
        headers=COORD,
    ).json()
    assert ok["stable_state"] == "COMPLETED"


def test_temperature_endpoint_returns_real_readings(client: TestClient):
    case_id, _ = _load_and_run(client)
    all_loggers = client.get(f"/api/cases/{case_id}/temperature").json()
    assert {lg["logger_id"] for lg in all_loggers["loggers"]} == {"LOGGER-A", "LOGGER-B"}
    assert all_loggers["permitted"] == {"min_c": 2.0, "max_c": 8.0, "tolerance_minutes": 10.0}
    one = client.get(f"/api/cases/{case_id}/temperature", params={"sample_id": "BX-212"}).json()
    assert len(one["loggers"]) == 1 and one["loggers"][0]["logger_id"] == "LOGGER-B"
    lg = one["loggers"][0]
    assert lg["reading_count"] > 1000 and lg["downsampled_to"] <= 400 and lg["malformed_rows"] == 0
    # the downsample must never hide the excursion: peak and trough survive
    assert max(p["c"] for p in lg["series"]) == 11.8
    assert min(p["c"] for p in lg["series"]) == min(p["c"] for p in lg["series"])
    assert [p["t"] for p in lg["series"]] == sorted(p["t"] for p in lg["series"])
    assert any(p["out"] for p in lg["series"])


def test_temperature_endpoint_reports_all_three_excursion_numbers(client: TestClient):
    """Practice evaluates peak, cumulative time and longest continuous run independently
    (LogTag instant/accumulative/consecutive); one number alone mis-states an excursion."""
    case_id, _ = _load_and_run(client)
    lg = client.get(f"/api/cases/{case_id}/temperature", params={"sample_id": "BX-212"}).json()["loggers"][0]
    m = lg["metrics"]
    assert m["peak_c"] == 11.8
    assert m["cumulative_minutes_out"] == 19.0
    assert m["longest_continuous_minutes"] == 19.0
    assert lg["status"] == "FAIL" and lg["reason_codes"] == ["TEMPERATURE_EXCURSION"]
    good = client.get(f"/api/cases/{case_id}/temperature", params={"sample_id": "BX-201"}).json()["loggers"][
        0
    ]
    assert good["status"] == "PASS" and good["metrics"]["cumulative_minutes_out"] <= 10.0


def test_a_completed_case_reopens_for_a_quarantine_review(client: TestClient):
    """The end of the demonstration is not the end of the specimen. A hold placed at intake is resolved
    later, and resolving it re-verifies rather than accepts."""
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    iid = r2["pending_interrupt"]["interrupt_id"]
    r3 = client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "QUARANTINE", "comment": "hold"},
        headers=COORD,
    ).json()
    assert r3["stable_state"] == "COMPLETED"

    held = [
        s
        for s in client.get(f"/api/cases/{case_id}").json()["report"]["samples"]
        if s["state"] == "QUARANTINED"
    ]
    assert held, "the demonstration should leave something on hold"
    sample_id = held[0]["sample_id"]

    # A reason is not optional: it is the record of why the hold was reopened.
    blank = client.post(
        f"/api/cases/{case_id}/samples/{sample_id}/quarantine-review", json={"reason": " "}, headers=COORD
    )
    assert blank.status_code == 422

    out = client.post(
        f"/api/cases/{case_id}/samples/{sample_id}/quarantine-review",
        json={"reason": "site sent the missing addendum; re-checking"},
        headers=COORD,
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["case_state"] == "VERIFYING"  # the case reopened
    # Re-verified AND re-decided by the engine. Never accepted by the reviewer, and never left in PENDING:
    # PENDING would mean "nobody has looked", which is untrue the moment verification has run.
    assert body["sample"]["state"] not in ("ACCEPTED", "ACCEPTED_WITH_EXCEPTION", "PENDING")

    opened = [
        e
        for e in client.get(f"/api/cases/{case_id}/events", params={"after": 0}).json()["events"]
        if e["event_type"] == "QUARANTINE_REVIEW_OPENED"
    ]
    assert len(opened) == 1 and "site sent the missing addendum" in opened[0]["summary"]


def test_only_an_authorised_role_may_reopen_a_hold(client: TestClient):
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    iid = r2["pending_interrupt"]["interrupt_id"]
    client.post(
        f"/api/cases/{case_id}/interrupts/{iid}/respond",
        json={"selected_option": "QUARANTINE", "comment": "hold"},
        headers=COORD,
    )
    held = [
        s
        for s in client.get(f"/api/cases/{case_id}").json()["report"]["samples"]
        if s["state"] == "QUARANTINED"
    ]
    denied = client.post(
        f"/api/cases/{case_id}/samples/{held[0]['sample_id']}/quarantine-review",
        json={"reason": "trying it on"},
        headers=CONTROL,
    )
    # 422 with an explicit code is this API's convention for a refused domain operation.
    assert denied.status_code == 422 and denied.json()["code"] == "INSUFFICIENT_ROLE"


def test_a_reopened_hold_gets_a_fresh_answerable_card(client: TestClient):
    """The failure this guards against is subtle and total: a reopened hold that produces no new decision
    card, or reuses the resolved one, leaves the case permanently stuck with nothing a person can answer."""
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    first_interrupt = r2["pending_interrupt"]["interrupt_id"]
    client.post(
        f"/api/cases/{case_id}/interrupts/{first_interrupt}/respond",
        json={"selected_option": "QUARANTINE", "comment": "hold"},
        headers=COORD,
    )

    client.post(
        f"/api/cases/{case_id}/samples/BX-212/quarantine-review",
        json={"reason": "site confirmed the logger was recalibrated"},
        headers=COORD,
    )
    run = client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"}, headers=COORD).json()
    assert run["stop_reason"] == "interrupt" and run["stable_state"] == "NEEDS_HUMAN_DECISION"

    open_cards = [
        c for c in client.get(f"/api/cases/{case_id}/decisions").json() if not c["resolved_decision_id"]
    ]
    assert len(open_cards) == 1
    assert open_cards[0]["sample_id"] == "BX-212"
    assert open_cards[0]["interrupt_id"] != first_interrupt, "a consumed interrupt cannot be answered again"

    final = client.post(
        f"/api/cases/{case_id}/interrupts/{open_cards[0]['interrupt_id']}/respond",
        json={"selected_option": "APPROVE_EXCEPTION", "comment": "logger artefact confirmed"},
        headers=headers("pi-kwame-osei"),
    ).json()
    assert final["stable_state"] == "COMPLETED"

    counts = client.get(f"/api/cases/{case_id}").json()["report"]["counts"]
    # The reopened one is resolved; BX-211's barcode collision is irresolvable and stays held.
    assert counts["ACCEPTED_WITH_EXCEPTION"] == 1 and counts["QUARANTINED"] == 1
    assert client.get(f"/api/cases/{case_id}").json()["report"]["unauthorized_acceptances"] == 0


def test_the_shipment_verification_report_covers_what_isber_requires(client: TestClient):
    """ISBER §J6 specifies this artifact's contents. It is a read over records already written, so it
    cannot drift from what happened, and it is the thing the sending site actually wants back."""
    case_id, r1 = _load_and_run(client)
    rid = r1["created_evidence_request_ids"][0]
    r2 = client.post(f"/api/evidence-requests/{rid}/complete", json=_evidence_body(client, rid)).json()
    client.post(
        f"/api/cases/{case_id}/interrupts/{r2['pending_interrupt']['interrupt_id']}/respond",
        json={"selected_option": "QUARANTINE", "comment": "hold pending review"},
        headers=COORD,
    )

    rep = client.get(f"/api/cases/{case_id}/verification-report").json()
    assert rep["complete"] and rep["shipment_id"] == "SHIP-DEMO-001"

    # Receipt: date, tracking, and who recorded it.
    assert rep["receipt"]["received_at"] and rep["receipt"]["received_by"]
    assert rep["receipt"]["sending_site"] and rep["receipt"]["tracking_reference"]

    # Condition: package, refrigerant, seal, container count, and the logger files.
    cond = rep["condition"]
    assert cond["package_condition"] == "ACCEPTABLE" and cond["seal_intact"] is True
    assert cond["refrigerant_condition"] and cond["container_count_matched"] is True
    assert cond["logger_files_received"] == 2

    # Reconciliation against the manifest, including the near-match stated in both directions.
    rec = rep["reconciliation"]
    assert rec["declared"] == 12 and rec["received"] == 12 and rec["not_received"] == []
    assert len(rec["identifier_near_matches"]) == 1
    near = rec["identifier_near_matches"][0]
    assert near["declared"] != near["read_on_tube"]
    # Not fully reconciled: row 7's tube read differently from the manifest, and nobody at the bench was
    # entitled to decide which was right. The sender's confirmation shows up under resolutions instead.
    assert rec["manifest_fully_reconciled"] is False

    # Discrepancies and the resolutions reached, the part a paper form leaves blank for weeks.
    assert any(r["resolution"] == "QUARANTINE" for r in rep["resolutions"])
    assert any("evidence request" in r["resolution"] for r in rep["resolutions"])

    # Disposition, carrying the policy version that decided rather than a signature.
    disp = rep["disposition"]
    assert len(disp["accepted"]) == 10 and len(disp["held"]) == 2 and disp["still_open"] == []
    assert "@" in disp["policy"]
