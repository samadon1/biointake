"""Acting on a case requires a credential, and the credential decides who you are.

The audit trail is the product here: every acceptance, exception and rejection is attributed to a
person. That attribution is worth nothing if the person is whoever the caller says they are.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from authn import TOKENS, USERS_SPEC, headers
from biointake.api.app import create_app
from biointake.api.config import Settings
from biointake.domain.enums import ActorRole
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from biointake.repositories.memory import InMemoryRepository
from biointake.services.auth import AuthenticationError, AuthService, hash_token


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


def test_every_lab_route_refuses_an_anonymous_caller(app):
    anonymous = TestClient(app)
    for method, path in [
        ("GET", "/api/cases"),
        ("GET", "/api/studies"),
        ("POST", "/api/demo/load"),
        ("GET", "/api/cases/CASE-SHIP-DEMO-001"),
        ("GET", "/api/cases/CASE-SHIP-DEMO-001/events"),
        ("GET", "/api/cases/CASE-SHIP-DEMO-001/outbox"),
    ]:
        r = anonymous.request(method, path)
        assert r.status_code == 401, f"{method} {path} answered {r.status_code} to nobody"


def test_the_sender_portal_stays_open_because_the_link_is_the_credential(app):
    """A sending site has no lab account; the single-use token in its link is what authorises it."""
    anonymous = TestClient(app)
    r = anonymous.get("/api/evidence-requests/REQ-does-not-exist")
    assert r.status_code != 401


def test_a_revoked_token_stops_working(app):
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    assert client.get("/api/cases").status_code == 200
    app.state.biointake.auth.revoke("coordinator-ama-asante")
    assert client.get("/api/cases").status_code == 401


def test_the_stored_credential_is_only_a_hash(app):
    user = app.state.biointake.services.repo.get_user("pi-kwame-osei")
    token = TOKENS["pi-kwame-osei"]
    assert token not in user.model_dump_json()
    assert user.token_sha256 == hash_token(token)


def test_authoring_a_study_needs_the_role_not_merely_a_credential(app):
    package = load_package(DEFAULT_FIXTURE_DIR)
    body = {
        "study_id": "NEW-01",
        "name": "A study a coordinator should not be able to open",
        "policy": json.loads(package.policy.model_copy(update={"protocol_id": "NEW-01"}).to_json()),
    }
    coordinator = TestClient(app, headers=headers("coordinator-ama-asante"))
    assert coordinator.post("/api/studies", json=body).status_code == 403
    pi = TestClient(app, headers=headers("pi-kwame-osei"))
    assert pi.post("/api/studies", json=body).status_code == 200


def test_a_deployment_refuses_to_start_without_configured_users():
    with pytest.raises(RuntimeError, match="BIOINTAKE_USERS"):
        create_app(Settings(backend="aws", ddb_table="nope", s3_bucket="nope"))


def test_a_reset_keeps_the_session_and_the_store_in_agreement(app):
    """Resetting the demo replaces the repository. The credentials must move with it.

    They did not: authentication kept reading the repository that had been thrown away, so a
    coordinator stayed signed in while every lookup of who they were came back empty.
    """
    client = TestClient(app, headers=headers("coordinator-ama-asante"))
    assert client.post("/api/demo/reset").status_code == 200
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "Ama Asante (receiving coordinator)"
    assert app.state.biointake.services.repo.get_user("coordinator-ama-asante") is not None


def test_one_click_sign_in_is_absent_unless_the_deployment_asks_for_it(app):
    """A lab must never ship a panel that hands out its principal investigator's credential."""
    assert TestClient(app).get("/api/demo/identities").status_code == 404


@pytest.fixture
def reviewable(tmp_path: Path):
    return create_app(
        Settings(
            backend="memory",
            invoker="local",
            session_dir=tmp_path / "sessions",
            users_spec=USERS_SPEC,
            demo_sign_in=True,
        )
    )


def test_the_panel_offers_people_and_hands_over_a_real_token(reviewable):
    listed = TestClient(reviewable).get("/api/demo/identities").json()
    assert {w["user_id"] for w in listed} == {
        "coordinator-ama-asante",
        "pi-kwame-osei",
        "qa-efua-boateng",
    }, "the control plane is not a person to sign in as"

    # The token is not a shortcut past authentication; it is the thing authentication reads.
    pi = next(w for w in listed if w["user_id"] == "pi-kwame-osei")
    client = TestClient(reviewable, headers={"Authorization": f"Bearer {pi['token']}"})
    me = client.get("/api/me").json()
    assert me["user_id"] == "pi-kwame-osei"
    assert me["role"] == "PRINCIPAL_INVESTIGATOR"


def test_signing_in_from_the_panel_does_not_flatten_the_roles(reviewable):
    listed = TestClient(reviewable).get("/api/demo/identities").json()
    tokens = {w["user_id"]: w["token"] for w in listed}
    package = load_package(DEFAULT_FIXTURE_DIR)
    body = {
        "study_id": "REVIEW-01",
        "name": "Authored from the review panel",
        "policy": json.loads(package.policy.model_copy(update={"protocol_id": "REVIEW-01"}).to_json()),
    }
    coordinator = TestClient(
        reviewable, headers={"Authorization": f"Bearer {tokens['coordinator-ama-asante']}"}
    )
    assert coordinator.post("/api/studies", json=body).status_code == 403
    pi = TestClient(reviewable, headers={"Authorization": f"Bearer {tokens['pi-kwame-osei']}"})
    assert pi.post("/api/studies", json=body).status_code == 200


def test_the_repository_still_holds_only_hashes(reviewable):
    """Offering the token in memory must not have put it in the store."""
    listed = TestClient(reviewable).get("/api/demo/identities").json()
    for who in listed:
        user = reviewable.state.biointake.services.repo.get_user(who["user_id"])
        assert who["token"] not in user.model_dump_json()
        assert user.token_sha256 == hash_token(who["token"])


class MovableClock:
    """A clock a test can move. SteppingClock advances on every read, which is the wrong shape for
    asking what happens thirty-one days later."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _auth(clock: MovableClock, lifetime: timedelta) -> AuthService:
    return AuthService(InMemoryRepository(clock), clock, lifetime=lifetime)


def test_a_credential_stops_working_when_it_expires():
    """Revocation only helps once somebody notices. An expiry does not need anybody to notice."""
    clock = MovableClock(datetime(2026, 8, 28, 9, 0, tzinfo=UTC))
    auth = _auth(clock, timedelta(days=30))
    token = auth.issue("coordinator-ama-asante", "Ama Asante", ActorRole.COORDINATOR)

    assert auth.authenticate(f"Bearer {token}").actor_id == "coordinator-ama-asante"

    clock.advance(timedelta(days=31))
    with pytest.raises(AuthenticationError, match="expired"):
        auth.authenticate(f"Bearer {token}")


def test_rotating_a_credential_restarts_its_clock():
    """A replacement should be good for a full term, not the remainder of the one it replaced."""
    clock = MovableClock(datetime(2026, 8, 28, 9, 0, tzinfo=UTC))
    auth = _auth(clock, timedelta(days=30))
    auth.issue("pi-kwame-osei", "Kwame Osei", ActorRole.PRINCIPAL_INVESTIGATOR)

    clock.advance(timedelta(days=29))
    replacement = auth.issue("pi-kwame-osei", "Kwame Osei", ActorRole.PRINCIPAL_INVESTIGATOR)

    clock.advance(timedelta(days=16))
    assert auth.authenticate(f"Bearer {replacement}").actor_id == "pi-kwame-osei"


def test_a_deployment_may_choose_not_to_expire_credentials():
    clock = MovableClock(datetime(2026, 8, 28, 9, 0, tzinfo=UTC))
    auth = _auth(clock, timedelta(0))
    token = auth.issue("qa-efua-boateng", "Efua Boateng", ActorRole.QA_REVIEWER)

    clock.advance(timedelta(days=3650))
    assert auth.authenticate(f"Bearer {token}").actor_id == "qa-efua-boateng"
