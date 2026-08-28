"""A lab registers its own sites, and a request that never left the building says so.

Both gaps had the same shape: the system asserted something it had not done. Contacts came only
from a demo fixture, so a real lab had nobody it was allowed to write to; and "sent" meant an audit
line, so an evidence request that reached nobody was recorded as though it had.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from authn import USERS_SPEC, headers
from biointake.api.app import create_app
from biointake.api.config import Settings, build_delivery
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.services.delivery import Delivery, RecordedDelivery, SesDelivery

NEW_CONTACT = {
    "contact_id": "SITE-CONTACT-900",
    "site_id": "SITE-KUMASI",
    "display_name": "Yaa Mensimah (site coordinator)",
    "destination": "yaa@example.invalid",
    "shipment_ids": ["SHIP-KUMASI-01"],
}


@pytest.fixture
def app(tmp_path: Path):
    return create_app(
        Settings(
            backend="memory",
            invoker="local",
            session_dir=tmp_path / "sessions",
            deterministic_clock=True,
            users_spec=USERS_SPEC,
        )
    )


def test_a_lab_can_register_a_site_it_has_verified(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    created = client.post("/api/contacts", json=NEW_CONTACT)
    assert created.status_code == 200, created.text
    assert created.json()["role"] == "SITE_CONTACT"
    listed = client.get("/api/contacts", params={"shipment_id": "SHIP-KUMASI-01"}).json()
    assert [c["contact_id"] for c in listed] == ["SITE-CONTACT-900"]


def test_registering_a_contact_is_recorded_against_the_person_who_did_it(app):
    client = TestClient(app, headers=headers("qa-efua-boateng"))
    client.post("/api/contacts", json=NEW_CONTACT)
    events = client.get("/api/configuration-events/contact:SITE-CONTACT-900").json()["events"]
    assert any(e["event_type"] == "CONTACT_REGISTERED" and e["actor_id"] == "qa-efua-boateng" for e in events)


def test_a_contact_id_cannot_be_moved_to_another_site(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    client.post("/api/contacts", json=NEW_CONTACT)
    moved = client.post("/api/contacts", json={**NEW_CONTACT, "site_id": "SITE-SOMEWHERE-ELSE"})
    assert moved.status_code == 409


def test_a_filed_message_is_not_reported_as_delivered():
    outcome = RecordedDelivery().send(to="someone@example.invalid", subject="s", body="b")
    assert not outcome.delivered
    assert "does not send mail" in outcome.detail


def test_the_outbox_says_whether_the_site_actually_has_it(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    client.post("/api/demo/reset")
    case_id = client.post("/api/demo/load").json()["case_id"]
    client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"})
    message = client.get(f"/api/cases/{case_id}/outbox").json()[0]
    assert message["delivered"] is False
    assert message["delivery_channel"] == "recorded"


def test_a_send_that_fails_does_not_become_a_send_that_worked():
    class RefusingSes:
        def client(self, _name):
            class Refuses:
                def send_email(self, **_kwargs):
                    raise RuntimeError("Email address is not verified")

            return Refuses()

    outcome = SesDelivery("lab@example.invalid", RefusingSes()).send(to="a@b.invalid", subject="s", body="b")
    assert not outcome.delivered and "not verified" in outcome.detail


def test_sending_for_real_must_be_asked_for_and_configured():
    assert isinstance(build_delivery(Settings(backend="memory")), RecordedDelivery)
    with pytest.raises(RuntimeError, match="BIOINTAKE_MAIL_FROM"):
        build_delivery(Settings(backend="memory", delivery="ses"))


def test_the_link_is_absolute_only_when_the_deployment_knows_its_own_address(app):
    """A link to localhost inside an email that did leave the building is worse than no link."""
    services = app.state.biointake.services
    assert services.portal_base_url == ""
    request = type("R", (), {"request_id": "REQ-0001", "upload_token": "tok", "body": ""})()
    assert services.portal_url(request) == "/portal/REQ-0001?token=tok"
    services.portal_base_url = "https://intake.example.org"
    assert services.portal_url(request) == "https://intake.example.org/portal/REQ-0001?token=tok"


def test_delivery_metadata_reaches_the_audit_trail(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    client.post("/api/demo/reset")
    case_id = client.post("/api/demo/load").json()["case_id"]
    client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"})
    events = client.get(f"/api/cases/{case_id}/events").json()["events"]
    sent = [e for e in events if e["event_type"] == "EVIDENCE_REQUEST_SENT"]
    assert sent, "no request was recorded at all"
    assert sent[0]["metadata"]["delivered"] is False
    assert "prepared for" in sent[0]["summary"]


def test_delivery_result_is_data_not_an_exception():
    assert Delivery(delivered=True, channel="ses", detail="ok", message_id="m").audit_metadata() == {
        "delivery_channel": "ses",
        "delivered": True,
        "delivery_detail": "ok",
        "provider_message_id": "m",
    }


def test_authoring_a_study_is_readable_in_the_same_place(app):
    """The study audit had the same problem: written, then unreachable."""
    client = TestClient(app, headers=headers("pi-kwame-osei"))
    studies = client.get("/api/studies").json()
    scope = f"study:{studies[0]['study_id']}"
    events = client.get(f"/api/configuration-events/{scope}").json()["events"]
    assert any(e["event_type"] == "STUDY_SAVED" for e in events)


def test_a_case_is_not_a_configuration_scope(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    assert client.get("/api/configuration-events/CASE-SHIP-DEMO-001").status_code == 404


def test_the_message_carries_a_link_the_recipient_can_open(tmp_path):
    """A site that receives the request must be able to act on it without asking for the link."""
    app = create_app(
        Settings(
            backend="memory",
            invoker="local",
            session_dir=tmp_path / "sessions",
            users_spec=USERS_SPEC,
            portal_base_url="https://intake.example.org",
        )
    )
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    client.post("/api/demo/reset")
    case_id = client.post("/api/demo/load").json()["case_id"]
    client.post(f"/api/cases/{case_id}/run", json={"event_type": "CASE_READY"})
    message = client.get(f"/api/cases/{case_id}/outbox").json()[0]
    token = message["portal_path"].split("token=")[1]
    assert f"https://intake.example.org/portal/{message['request_id']}?token={token}" in message["body"]


def test_a_reset_leaves_the_lab_able_to_receive(app):
    """Reset purges the lab's own configuration. It has to put it back.

    On the AWS backend the purge removes the CONTACTS and LIMS partitions and the in-memory branch's
    rebuild does not apply. Without a re-seed the verified directory is empty, every announcement is
    refused for want of a verified sender, and the deployment stays broken until it is restarted.
    """
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    assert client.post("/api/demo/reset").status_code == 200
    contacts = client.get("/api/contacts").json()
    assert [c["contact_id"] for c in contacts], "no verified contact survived the reset"
    assert app.state.biointake.services.lims.records(), "the lab's record system is empty"

    # And the records have to be readable by a fresh query, not merely present in a cache the purge
    # went around. That was the failure: the store reported records it no longer held, seeding
    # skipped them as already there, and every specimen came back LIMS_RECORD_MISSING.
    package = load_package(DEFAULT_FIXTURE_DIR)
    expected = {r.sample_id for r in package.lims_records}
    lims = app.state.biointake.services.lims
    assert {r.sample_id for r in lims.records()} >= expected


def test_a_reset_does_not_leave_ids_that_will_be_minted_again(app):
    """Counters are reset so a fresh run produces the same ids. Nothing may still be holding them.

    Purging only the demonstration case while resetting the counters left earlier cases owning
    SCAN-0001 onwards. The next shipment minted those same ids, collided with records that were
    still there, and the receiving bench reported every tube as already scanned, with no scan
    event anywhere to explain it.
    """
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    client.post("/api/demo/reset")
    client.post("/api/demo/load")
    assert client.get("/api/cases").json(), "nothing to purge; the test proves nothing"

    client.post("/api/demo/reset")
    assert client.get("/api/cases").json() == [], "a case survived the reset holding reusable ids"
