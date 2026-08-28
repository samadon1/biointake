"""FastAPI control API.

Trusted context (actor identity/role, event ids, session ids, upload tokens) is established here and
handed to the agent as a typed InvocationEvent. The browser never talks to the agent directly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

from ..agent.events import EvidenceDelivery, InvocationEvent, RunResult
from ..domain.commands import (
    IncomingArtifact,
    OpenQuarantineReviewCommand,
    ProposedCorrection,
    derive_operation_id,
)
from ..domain.enums import (
    ActorRole,
    ActorType,
    AuditEventType,
    EvidenceRequestStatus,
    InvocationEventType,
    PackageCondition,
    ReceivedQuality,
)
from ..domain.errors import BioIntakeError, NotFoundError
from ..domain.models import ActorContext, SiteContact, Study
from ..domain.policies import ProtocolPolicy
from ..services.auth import AuthenticationError, AuthService, mint_token
from ..services.demo_ramp import play_demo_through_ramp
from ..services.intake import IntakeService
from ..services.intake_ramp import IntakeRampService
from ..services.verification_report import build_verification_report
from .config import Settings, build_services, demo_package
from .invokers import AgentCoreInvoker, AgentInvoker, LocalInvoker

LEASE_TTL_SECONDS = 300

# Demo identity directory: the server decides who a caller is; clients cannot claim a role.
# The lab's staff. Who they are is configuration; what proves they are them is a token, which is
# never in this file. See AppState._bootstrap_users.
DEFAULT_STAFF: dict[str, tuple[str, ActorRole]] = {
    "coordinator-ama-asante": ("Ama Asante (receiving coordinator)", ActorRole.COORDINATOR),
    "pi-kwame-osei": ("Kwame Osei (principal investigator)", ActorRole.PRINCIPAL_INVESTIGATOR),
    "qa-efua-boateng": ("Efua Boateng (QA reviewer)", ActorRole.QA_REVIEWER),
    "control-plane": ("BioIntake control plane", ActorRole.SYSTEM),
}


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.running: set[str] = set()  # cases with an invocation in flight (drives the live UI)
        self.services: IntakeService = build_services(settings)
        self.ramp = IntakeRampService(self.services.repo, self.services.storage, self.services.clock)
        self.issued_tokens: dict[str, str] = {}  # only populated when tokens are minted locally
        self._bootstrap_users()
        self._seed_lab_reference_data()
        self.invoker: AgentInvoker = (
            AgentCoreInvoker(settings)
            if settings.invoker == "agentcore"
            else LocalInvoker(settings, self.services)
        )

    def _bootstrap_users(self) -> None:
        """Establish who may act, and what proves it.

        In a deployment the tokens come from the environment (Secrets Manager on App Runner), and the
        repository never sees a plaintext one. Running locally against the in-memory backend there is
        nothing to read them from, so fresh random tokens are minted and printed once; a developer
        signs in with one exactly as a coordinator would. There is no bypass for either case.
        """
        # Bound to the repository that exists now: a reset replaces it, and an auth service still
        # reading the old one would authenticate people the rest of the system no longer knows.
        self.auth = AuthService(self.services.repo, self.services.clock)
        if self.settings.users_spec.strip():
            for entry in self.settings.users_spec.split(";"):
                if not entry.strip():
                    continue
                parts = [p.strip() for p in entry.split("|")]
                if len(parts) != 4 or not all(parts):
                    raise RuntimeError("BIOINTAKE_USERS entries must be 'user_id|Display Name|ROLE|token'")
                user_id, display_name, role, token = parts
                self.auth.issue(user_id, display_name, ActorRole(role), token)
                if self.settings.demo_sign_in:
                    # Held in memory, and only because this deployment has been asked to offer it.
                    # The repository still stores nothing but the hash.
                    self.issued_tokens[user_id] = token
            return
        if self.settings.backend != "memory":
            raise RuntimeError(
                "BIOINTAKE_USERS is not set. A deployed BioIntake refuses to start without the "
                "credentials of the people allowed to act on cases."
            )
        already_minted = bool(self.issued_tokens)
        for user_id, (display_name, role) in DEFAULT_STAFF.items():
            # Re-seeding after a reset keeps the tokens already handed out, so resetting the demo
            # does not sign the coordinator out of the browser they are watching it in.
            self.issued_tokens[user_id] = self.auth.issue(
                user_id, display_name, role, self.issued_tokens.get(user_id)
            )
        if already_minted:
            return
        print("BioIntake local sign-in tokens (regenerated on every start):", file=sys.stderr)
        for user_id, token in self.issued_tokens.items():
            print(f"  {user_id:26} {token}", file=sys.stderr)

    def _seed_lab_reference_data(self) -> None:
        """A receiving lab's own configuration: the studies it runs, the site contacts it has
        verified, and its own record system.

        This is not shipment data; it exists before any box arrives, and a site must be able to
        announce a shipment against it without a demo case having been loaded first. The LIMS used
        to be seeded by the demo loader instead, which meant a shipment announced by hand was
        reconciled against an empty record system: LIMS_RECONCILIATION came back UNAVAILABLE on
        every specimen, nothing could be accepted, and the accession collision the lab is supposed
        to catch did not exist to catch.
        """
        package = demo_package(self.settings)
        self.ramp.ensure_default_study(self.services.policy)
        for contact in package.contacts:
            self.services.repo.save_contact(contact)
        if package.lims_records:
            self.services.lims.seed(package.lims_records)

    def _clear_sessions(self) -> None:
        """Drop persisted Strands sessions: a reset case must not inherit an old pending interrupt."""
        if self.settings.backend == "aws":
            try:
                s3 = self.settings.boto_session().client("s3")
                for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=self.settings.s3_bucket, Prefix="sessions/"
                ):
                    for obj in page.get("Contents", []):
                        s3.delete_object(Bucket=self.settings.s3_bucket, Key=obj["Key"])
            except Exception:  # noqa: BLE001, best effort; a stale session must not block a new case
                pass
        else:
            shutil.rmtree(self.settings.session_dir, ignore_errors=True)

    def reset(self) -> dict[str, Any]:
        package = demo_package(self.settings)
        case_id = f"CASE-{package.shipment.shipment_id}"
        self._clear_sessions()
        if self.settings.backend == "memory":
            self.services = build_services(self.settings)
            self.ramp = IntakeRampService(self.services.repo, self.services.storage, self.services.clock)
            self._bootstrap_users()
            self._seed_lab_reference_data()
            self.invoker = (
                LocalInvoker(self.settings, self.services)
                if self.settings.invoker == "local"
                else self.invoker
            )
            return {"reset": "memory", "case_id": case_id}
        from ..repositories.dynamodb import DynamoDBRepository

        repo = self.services.repo
        assert isinstance(repo, DynamoDBRepository)
        # Every case, not just the demonstration one. The counters are reset so a fresh run produces
        # the same ids, and that is only safe if nothing still holds the ids about to be minted
        # again. Purging one case and resetting the counters left earlier cases owning SCAN-0001
        # onwards; the next shipment minted those same ids, collided with records that were still
        # there, and the bench reported every tube as already scanned, with no scan event to
        # explain it. A reset resets.
        n = sum(repo.purge_case(c.case_id) for c in list(repo.list_cases()))
        n += repo.purge_case("LIMS") + repo.purge_case("CONTACTS") + repo.purge_counters()
        # Everything holding state is rebuilt against the emptied table before anything is put back.
        #
        # Re-seeding alone was not enough. The purge deletes rows through the repository, behind the
        # LIMS store's read-through cache, so the cache went on reporting records that were no longer
        # there, and seeding skips a record it believes it already has. The result was a lab whose
        # record system looked populated to itself and was empty to every query: LIMS_RECONCILIATION
        # came back UNAVAILABLE on every specimen and nothing could be accepted. After a purge,
        # nothing held in memory is worth trusting.
        self.services = build_services(self.settings)
        self.ramp = IntakeRampService(self.services.repo, self.services.storage, self.services.clock)
        self._bootstrap_users()
        self._seed_lab_reference_data()
        if self.settings.invoker == "local":
            self.invoker = LocalInvoker(self.settings, self.services)
        return {"reset": "aws", "case_id": case_id, "items_deleted": n}


# ---------------------------------------------------------------------------------------------
class RunRequest(BaseModel):
    event_type: InvocationEventType = InvocationEventType.CASE_READY


class UploadFile(BaseModel):
    filename: str
    mime_type: str
    content_base64: str


class CorrectionIn(BaseModel):
    manifest_row: int
    manifest_value: str
    corrected_value: str
    sender_statement: str = ""


class CompleteEvidenceRequest(BaseModel):
    upload_token: str
    submitted_by_contact_id: str
    sender_message: str = Field(default="", max_length=4000)
    files: list[UploadFile] = Field(default_factory=list)
    proposed_corrections: list[CorrectionIn] = Field(
        default_factory=list
    )  # optional structured form; the agent may also extract from text


class StudyIn(BaseModel):
    """A study a lab authors, carrying the acceptance criteria its specimens will be judged against."""

    study_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    policy: dict[str, Any]
    site_ids: list[str] = Field(default_factory=list)
    relabelling_permitted: bool = True
    reconcile_before_storage: bool = True


class ContactIn(BaseModel):
    """A site contact the lab has verified out of band and is willing to write to."""

    contact_id: str = Field(min_length=1, max_length=64)
    site_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=3, max_length=320)
    shipment_ids: list[str] = Field(default_factory=list)


class AnnounceIn(BaseModel):
    """What a sending site declares before the courier is booked."""

    shipment_id: str
    study_id: str
    sender_site_id: str
    announced_by_contact_id: str
    manifest_csv_base64: str
    courier: str = ""
    tracking_reference: str = ""
    shipped_at: str | None = None
    expected_arrival: str | None = None
    container_count: int = Field(default=1, ge=1, le=200)
    logger_ids: list[str] = Field(default_factory=list)
    shipping_condition: str = ""
    # Both travel with the shipment: custody is the record of who held the box, and consent is held by
    # whoever enrolled the participant. Optional here because a site may not have them ready, in which case
    # their checks report UNAVAILABLE by name rather than the case failing silently.
    custody_log_base64: str = ""
    consent_records_base64: str = ""


class ValidateManifestIn(BaseModel):
    study_id: str
    manifest_csv_base64: str


class ReceiptIn(BaseModel):
    package_condition: str = "ACCEPTABLE"
    condition_notes: str = Field(default="", max_length=1000)
    package_count_received: int = Field(default=1, ge=0, le=200)
    refrigerant_condition: str = Field(default="", max_length=200)
    temperature_at_reception_c: float | None = None
    seal_intact: bool = True
    logger_files: list[UploadFile] = Field(default_factory=list)


class ScanIn(BaseModel):
    value: str = Field(min_length=1, max_length=120)
    container_id: str = Field(default="", max_length=60)
    encoded_barcode: str = Field(default="", max_length=120)


class BulkScanIn(BaseModel):
    """A column of identifiers pasted straight out of a rack scanner's client software."""

    text: str = Field(min_length=1, max_length=200_000)
    container_id: str = Field(default="", max_length=60)


class QualityIn(BaseModel):
    row: int = Field(ge=1)
    received_quality: str


class AccessionIn(BaseModel):
    row: int = Field(ge=1)
    encoded_barcode: str = Field(min_length=1, max_length=120)


class CommitBatchIn(BaseModel):
    accept_partial: bool = False


class QuarantineReviewIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DecisionIn(BaseModel):
    selected_option: str
    comment: str = Field(default="", max_length=500)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    st = state or AppState(settings)
    app = FastAPI(title="BioIntake control API", version="0.1.0")
    app.state.biointake = st
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            o.strip()
            for o in (
                os.environ.get("BIOINTAKE_CORS_ORIGINS") or "http://localhost:3000,http://127.0.0.1:3000"
            ).split(",")
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def svc() -> IntakeService:
        return st.services

    def actor(authorization: str | None = Header(default=None)) -> ActorContext:
        try:
            return st.auth.authenticate(authorization)
        except AuthenticationError as e:
            # One message for every failure: a caller learns whether it is signed in, never whether a
            # particular user id or token exists.
            raise HTTPException(
                status_code=401,
                detail="sign in with a bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from e

    def require_role(*roles: ActorRole) -> Callable[..., ActorContext]:
        def dependency(who: ActorContext = Depends(actor)) -> ActorContext:
            if who.role not in roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"{who.role.value} may not do this; requires {', '.join(r.value for r in roles)}",
                )
            return who

        return dependency

    @contextmanager
    def lease(case_id: str) -> Iterator[str]:
        owner = f"api-{uuid.uuid4().hex[:8]}"
        if not svc().repo.acquire_lease(case_id, owner, LEASE_TTL_SECONDS):
            raise HTTPException(
                status_code=409, detail=f"case {case_id} is being processed by another invocation"
            )
        try:
            yield owner
        finally:
            svc().repo.release_lease(case_id, owner)

    def run(event: InvocationEvent) -> RunResult:
        with lease(event.case_id):
            st.running.add(event.case_id)
            try:
                result = st.invoker.invoke(event)
            finally:
                st.running.discard(event.case_id)
        # remember which interrupt belongs to which decision card so /respond can validate it
        if result.pending_interrupt is not None:
            issue_id = str(result.pending_interrupt.reason.get("issue_id", ""))
            pending = svc().repo.get_pending_decision(issue_id) if issue_id else None
            if pending is not None and pending.interrupt_id != result.pending_interrupt.interrupt_id:
                svc().repo.save_pending_decision(
                    pending.model_copy(update={"interrupt_id": result.pending_interrupt.interrupt_id})
                )
        return result

    def get_case_or_404(case_id: str) -> Any:
        try:
            return svc().repo.get_case(case_id)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    # -- demo ----------------------------------------------------------------------------------
    @app.post("/api/demo/reset")
    def demo_reset(who: ActorContext = Depends(actor)) -> dict[str, Any]:
        return st.reset()

    @app.post("/api/demo/load")
    def demo_load(through_ramp: bool = True, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Load the demonstration shipment.

        By default this plays *through* the real intake ramp: the site announces the shipment and uploads its
        manifest, a tech records receipt and the logger files, every tube is scanned, and the staging batch is
        committed. What a viewer sees is the path a lab would actually take. `through_ramp=false` keeps the
        old fixture shortcut for tests that only care about the agent.
        """
        package = demo_package(settings)
        case_id = f"CASE-{package.shipment.shipment_id}"
        try:
            existing = svc().repo.get_case(case_id)
            return {
                "case_id": existing.case_id,
                "session_id": existing.agent_session_id,
                "state": existing.state.value,
                "created": False,
            }
        except NotFoundError:
            pass
        if not through_ramp:
            case = svc().create_case(package, ActorContext.system("control-plane"))
            svc().begin_verification(case.case_id, ActorContext.system("control-plane"))
            return {
                "case_id": case.case_id,
                "session_id": case.agent_session_id,
                "state": "VERIFYING",
                "created": True,
                "via": "fixture",
            }
        return {
            **play_demo_through_ramp(svc(), st.ramp, package, case_id),
            "created": True,
            "via": "intake-ramp",
        }

    @app.get("/api/demo/sender-reply")
    def demo_sender_reply(who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Demo helper for the sender portal: the fixture's reply text and addendum file (synthetic)."""
        package = demo_package(settings)
        reply = json.loads(package.later["sender-reply.json"])
        return {
            "from_contact_id": reply["from_contact_id"],
            "free_text": reply["free_text"],
            "files": [
                {
                    "filename": "consent-addendum.json",
                    "mime_type": "application/json",
                    "content_base64": base64.b64encode(package.later["consent-addendum.json"]).decode(),
                }
            ],
        }

    # -- studies (protocol configuration a lab owns) ---------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        """Unauthenticated on purpose: a load balancer has no credential, and this reveals nothing
        about the lab's data. It answers only once the repository is reachable, so an instance that
        cannot see its table is never sent traffic."""
        try:
            st.services.repo.list_studies()
        except Exception as e:  # noqa: BLE001, an unhealthy instance reports, it does not crash
            raise HTTPException(status_code=503, detail=f"repository unreachable: {type(e).__name__}") from e
        return {"status": "ok", "backend": st.settings.backend}

    @app.get("/api/demo/identities")
    def demo_identities() -> list[dict[str, Any]]:
        """The staff this deployment will sign you in as, with their tokens.

        Unauthenticated, because it is what gets you a credential in the first place, and present
        only where BIOINTAKE_DEMO_SIGN_IN is set. A deployment that offers this is a deployment
        anyone holding the URL can act on, which is the right trade for synthetic data being
        reviewed and the wrong one for a lab. Everything downstream is unchanged: the button hands
        over a real token, the server decides the role from it, and a coordinator still cannot
        author a study.
        """
        if not st.settings.demo_sign_in:
            raise HTTPException(status_code=404, detail="not found")
        out = []
        for user_id, (display_name, role) in DEFAULT_STAFF.items():
            token = st.issued_tokens.get(user_id)
            if not token or role is ActorRole.SYSTEM:
                continue  # the control plane is not a person to sign in as
            out.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "role": role.value,
                    "token": token,
                }
            )
        return out

    @app.get("/api/me")
    def whoami(who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Who the credential says you are. The client never decides this."""
        user = svc().repo.get_user(who.actor_id)
        return {
            "user_id": who.actor_id,
            "display_name": user.display_name if user else who.actor_id,
            "role": who.role.value,
        }

    @app.get("/api/contacts")
    def list_contacts(
        shipment_id: str | None = None, who: ActorContext = Depends(actor)
    ) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in svc().repo.list_contacts(shipment_id)]

    @app.post("/api/contacts")
    def create_contact(
        body: ContactIn,
        who: ActorContext = Depends(
            require_role(ActorRole.COORDINATOR, ActorRole.PRINCIPAL_INVESTIGATOR, ActorRole.QA_REVIEWER)
        ),
    ) -> dict[str, Any]:
        """Register a site contact.

        This directory is the only place the agent may take a destination from: it can choose among
        contacts by id, and can never supply an address of its own. Adding one is therefore a
        deliberate act by a member of the lab, which is why it is a route and not a tool.
        """
        existing = svc().repo.get_contact(body.contact_id)
        if existing is not None and existing.site_id != body.site_id:
            raise HTTPException(
                status_code=409,
                detail=f"contact {body.contact_id} already belongs to site {existing.site_id}",
            )
        shipments = tuple(dict.fromkeys([*(existing.shipment_ids if existing else ()), *body.shipment_ids]))
        contact = SiteContact(
            contact_id=body.contact_id,
            site_id=body.site_id,
            display_name=body.display_name,
            destination=body.destination,
            shipment_ids=shipments,
            role=ActorRole.SITE_CONTACT,
            active=True,
        )
        svc().repo.save_contact(contact)
        svc().repo.append_audit(
            case_id=f"contact:{contact.contact_id}",
            event_type=AuditEventType.CONTACT_REGISTERED,
            actor=who,
            summary=f"{who.actor_id} verified {contact.display_name} ({contact.contact_id}) at {contact.site_id}",
            metadata={"site_id": contact.site_id, "shipment_ids": list(shipments)},
        )
        return dict(contact.model_dump(mode="json"))

    @app.get("/api/studies")
    def list_studies(who: ActorContext = Depends(actor)) -> list[dict[str, Any]]:
        return [s.model_dump(mode="json") for s in svc().repo.list_studies()]

    @app.post("/api/studies")
    def create_study(body: StudyIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Author a study, meaning the acceptance criteria this lab's specimens are judged against.

        ISO 20387 §7.3.2.2 asks a biobank to define those criteria and verify them on reception. Until a
        lab can write them down, every decision is made against somebody else's protocol.
        """
        if who.role not in (ActorRole.PRINCIPAL_INVESTIGATOR, ActorRole.QA_REVIEWER):
            raise HTTPException(
                status_code=403,
                detail="a study defines what may be accepted, so authoring one is reserved to a "
                "principal investigator or a QA reviewer",
            )
        try:
            policy = ProtocolPolicy.model_validate(body.policy)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=f"policy is not valid: {e.errors()[0]}") from None
        if svc().repo.get_study(body.study_id) is not None:
            raise HTTPException(status_code=409, detail=f"study {body.study_id} already exists")

        now = svc().clock()
        study = Study(
            study_id=body.study_id,
            name=body.name,
            protocol_id=policy.protocol_id,
            policy_version=policy.version,
            policy=policy,
            site_ids=tuple(body.site_ids),
            relabelling_permitted=body.relabelling_permitted,
            exception_approval_role=(
                policy.temperature.exception_roles or (ActorRole.PRINCIPAL_INVESTIGATOR,)
            )[0],
            reconcile_before_storage=body.reconcile_before_storage,
            created_at=now,
            updated_at=now,
        )
        st.ramp.save_study(study, who)
        return study.model_dump(mode="json")

    # -- the intake ramp -------------------------------------------------------------------------
    @app.post("/api/manifests/validate")
    def validate_manifest(body: ValidateManifestIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Check a manifest against the study before anything ships. Catching a wrong specimen type here
        costs an email; catching it after the box has shipped costs a cold-chain excursion."""
        study = svc().repo.get_study(body.study_id)
        if study is None:
            raise HTTPException(status_code=404, detail=f"unknown study {body.study_id}")
        v = st.ramp.validate_manifest(base64.b64decode(body.manifest_csv_base64), svc().policy)
        return {
            "accepted": v.accepted,
            "summary": v.summary,
            "problems": list(v.problems),
            "warnings": list(v.warnings),
            "reason_codes": [c.value for c in v.reason_codes],
            "lines": [line.model_dump(mode="json") for line in v.lines],
        }

    @app.post("/api/shipments/announce")
    def announce_shipment(body: AnnounceIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        study = svc().repo.get_study(body.study_id)
        if study is None:
            raise HTTPException(status_code=404, detail=f"unknown study {body.study_id}")
        contact = svc().repo.get_contact(body.announced_by_contact_id)
        if contact is None or not contact.active:
            raise HTTPException(
                status_code=403, detail="announcements are accepted only from a verified site contact"
            )
        case, ann, validation = st.ramp.announce(
            case_id=f"CASE-{body.shipment_id}",
            shipment_id=body.shipment_id,
            study=study,
            policy=svc().policy,
            sender_site_id=body.sender_site_id,
            announced_by_contact_id=body.announced_by_contact_id,
            manifest_csv=base64.b64decode(body.manifest_csv_base64),
            courier=body.courier,
            tracking_reference=body.tracking_reference,
            shipped_at=_parse_dt(body.shipped_at),
            expected_arrival=_parse_dt(body.expected_arrival),
            container_count=body.container_count,
            logger_ids=tuple(body.logger_ids),
            shipping_condition=body.shipping_condition,
            custody_log=base64.b64decode(body.custody_log_base64) if body.custody_log_base64 else None,
            consent_records=(
                base64.b64decode(body.consent_records_base64) if body.consent_records_base64 else None
            ),
            actor=ActorContext(
                actor_type=ActorType.SENDER,
                actor_id=body.announced_by_contact_id,
                role=ActorRole.SITE_CONTACT,
            ),
        )
        return {
            "case_id": case.case_id,
            "state": case.state.value,
            "announcement": ann.model_dump(mode="json"),
            "declared_specimens": len(validation.lines),
        }

    @app.get("/api/cases/{case_id}/intake")
    def get_intake(case_id: str, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Everything the receiving bench needs: what was declared, what was recorded on arrival, and the
        expected rows with any scan against them."""
        case = get_case_or_404(case_id)
        ann = svc().repo.get_announcement(case_id)
        receipt = svc().repo.get_receipt(case_id)
        return {
            "case_id": case_id,
            "state": case.state.value,
            "announcement": ann.model_dump(mode="json") if ann else None,
            "receipt": receipt.model_dump(mode="json") if receipt else None,
            "batch": st.ramp.batch_summary(case_id) if ann else None,
        }

    @app.post("/api/cases/{case_id}/receipt")
    def record_receipt(case_id: str, body: ReceiptIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        get_case_or_404(case_id)
        try:
            condition = PackageCondition(body.package_condition)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"condition must be one of {[c.value for c in PackageCondition]}"
            ) from None
        receipt = st.ramp.record_receipt(
            case_id=case_id,
            actor=who,
            package_condition=condition,
            condition_notes=body.condition_notes,
            package_count_received=body.package_count_received,
            refrigerant_condition=body.refrigerant_condition,
            temperature_at_reception_c=body.temperature_at_reception_c,
            seal_intact=body.seal_intact,
            logger_files=tuple((f.filename, base64.b64decode(f.content_base64)) for f in body.logger_files),
        )
        return receipt.model_dump(mode="json")

    @app.post("/api/cases/{case_id}/scan")
    def scan_specimen(case_id: str, body: ScanIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        get_case_or_404(case_id)
        result = st.ramp.scan(
            case_id, body.value, who, container_id=body.container_id, encoded_barcode=body.encoded_barcode
        )
        return {**result.model_dump(mode="json"), "batch": st.ramp.batch_summary(case_id)}

    @app.post("/api/cases/{case_id}/scan/bulk")
    def scan_bulk(case_id: str, body: BulkScanIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Paste a whole rack. Split on commas, tabs and newlines, the three things every scanner export
        uses, and run each value through the same reconciliation a handheld read gets."""
        get_case_or_404(case_id)
        values = [v for v in re.split(r"[,\t\r\n;]+", body.text) if v.strip()]
        if len(values) > 2000:
            raise HTTPException(status_code=413, detail=f"{len(values)} values is more than one shipment")
        results = st.ramp.scan_many(case_id, values, who, container_id=body.container_id)
        return {
            "scanned": len(results),
            "results": [r.model_dump(mode="json") for r in results],
            "batch": st.ramp.batch_summary(case_id),
        }

    @app.post("/api/cases/{case_id}/accession")
    def attach_accession(
        case_id: str, body: AccessionIn, who: ActorContext = Depends(actor)
    ) -> dict[str, Any]:
        """Attach the site's own accession, read from the label's second barcode, to a scanned row."""
        get_case_or_404(case_id)
        st.ramp.attach_accession(case_id, body.row, body.encoded_barcode, who)
        return {"row": body.row, "batch": st.ramp.batch_summary(case_id)}

    @app.post("/api/cases/{case_id}/quality")
    def set_quality(case_id: str, body: QualityIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        get_case_or_404(case_id)
        try:
            quality = ReceivedQuality(body.received_quality)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"quality must be one of {[q.value for q in ReceivedQuality]}"
            ) from None
        st.ramp.amend_quality(case_id, body.row, quality, who)
        return {"row": body.row, "received_quality": quality.value, "batch": st.ramp.batch_summary(case_id)}

    @app.post("/api/cases/{case_id}/batch/commit")
    def commit_batch(case_id: str, body: CommitBatchIn, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        get_case_or_404(case_id)
        samples, summary = st.ramp.commit_batch(case_id, who, accept_partial=body.accept_partial)
        return {
            "committed": [s.sample_id for s in samples],
            "summary": summary,
            "state": svc().repo.get_case(case_id).state.value,
        }

    # -- cases ---------------------------------------------------------------------------------
    @app.get("/api/cases")
    def list_cases(who: ActorContext = Depends(actor)) -> list[dict[str, Any]]:
        out = []
        for c in svc().repo.list_cases():
            announcement = svc().repo.get_announcement(c.case_id)
            out.append(
                {
                    "case_id": c.case_id,
                    "shipment_id": c.shipment_id,
                    "state": c.state.value,
                    "samples": c.observed_sample_count,
                    # What the site said was coming, which is the only specimen count that exists before the
                    # box is opened. Reporting the observed count there would show a bare 0 and read as an
                    # error rather than as "not received yet".
                    "declared": len(announcement.expected_lines) if announcement else None,
                    "active_requests": len(svc().repo.list_requests(c.case_id, EvidenceRequestStatus.ACTIVE)),
                    "pending_decisions": len(svc().repo.list_pending_decisions(c.case_id)),
                    "updated_at": c.updated_at.isoformat(),
                }
            )
        return out

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        get_case_or_404(case_id)
        return {
            "snapshot": svc().snapshot(case_id),
            "report": svc().build_report(case_id),
            "checks": [
                {
                    "sample_id": c.sample_id,
                    "category": c.category.value,
                    "status": c.status.value,
                    "summary": c.summary,
                    "observed_value": c.observed_value,
                    "expected_value": c.expected_value,
                    "reason_codes": [r.value for r in c.reason_codes],
                    "evidence_refs": list(c.evidence_refs),
                    "provisional": c.provisional,
                    "rule_version": c.rule_version,
                    "evaluated_at": c.evaluated_at.isoformat(),
                }
                for c in svc().repo.current_checks(case_id)
            ],
            "agent_running": case_id in st.running,
        }

    @app.get("/api/cases/{case_id}/events")
    def get_events(case_id: str, after: int = 0, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        case = get_case_or_404(case_id)
        events = [a.model_dump(mode="json") for a in svc().repo.list_audit(case_id) if a.sequence > after]
        return {"events": events, "agent_running": case_id in st.running, "case_state": case.state.value}

    @app.get("/api/configuration-events/{scope}")
    def configuration_events(scope: str, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """Audit for something that is not a shipment: a study authored, a site contact verified.

        These were being recorded and then never shown: the case events route resolves a case first,
        and a study is not a case. A record nobody can read is not a record.
        """
        if not scope.startswith(("study:", "contact:")):
            raise HTTPException(status_code=404, detail="not a configuration scope")
        return {"events": [a.model_dump(mode="json") for a in svc().repo.list_audit(scope)]}

    @app.get("/api/cases/{case_id}/outbox")
    def get_outbox(case_id: str, who: ActorContext = Depends(actor)) -> list[dict[str, Any]]:
        """Every evidence request as the message that went out, with its secure link and whether it
        actually reached the recipient. Where the deployment does not send mail, this is the outbox a
        coordinator works from: the message is real, the link is real, only the sending is not."""
        get_case_or_404(case_id)
        out = []
        for r in svc().repo.list_requests(case_id):
            contact = svc().repo.get_contact(r.recipient_contact_id)
            out.append(
                {
                    "request_id": r.request_id,
                    "status": r.status.value,
                    "to": {
                        "contact_id": r.recipient_contact_id,
                        "display_name": contact.display_name if contact else None,
                        "destination": contact.destination if contact else None,
                    },
                    "subject": r.subject,
                    "body": r.body,
                    "sent_at": r.sent_at.isoformat(),
                    "delivered": r.delivered,
                    "delivery_channel": r.delivery_channel,
                    "delivery_detail": r.delivery_detail,
                    "portal_path": f"/portal/{r.request_id}?token={r.upload_token}",
                    "affected_sample_ids": list(r.affected_sample_ids),
                }
            )
        return out

    @app.get("/api/cases/{case_id}/temperature")
    def get_temperature(
        case_id: str,
        sample_id: str | None = None,
        max_points: int = 400,
        who: ActorContext = Depends(actor),
    ) -> dict[str, Any]:
        """Actual logger readings behind a temperature check, downsampled for display.

        The decision card shows the real trace, not a reconstruction: the numbers a person sees are
        the ones the deterministic evaluator read.
        """
        get_case_or_404(case_id)
        from ..domain.enums import ArtifactType
        from ..services.temperature import evaluate_logger, parse_logger_csv

        policy = svc().policy.temperature
        wanted: set[str] | None = None
        if sample_id:
            sample = svc().repo.get_sample(sample_id)
            wanted = {sample.logger_id} if sample.logger_id else set()
        loggers = []
        for art in svc().repo.list_artifacts(case_id, ArtifactType.TEMPERATURE_LOG):
            logger_id = str(art.metadata.get("logger_id", art.original_filename))
            if wanted is not None and logger_id not in wanted:
                continue
            data = svc().storage.get(art.storage_uri)
            readings, malformed = parse_logger_csv(data)
            # Practice evaluates three independent numbers, peak, cumulative time out of range and the
            # longest continuous run (LogTag's "instant" / "accumulative" / "consecutive" alarm types).
            # Showing only one understates or overstates an excursion depending on its shape.
            summary = evaluate_logger(logger_id, data, policy)
            # Extreme-preserving downsample: a plain stride can drop the peak and make an excursion
            # look smaller than it was. Keep each bucket's min and max, in time order.
            bucket = max(1, -(-len(readings) // max(1, max_points // 2)))
            kept = []
            for i in range(0, len(readings), bucket):
                window = readings[i : i + bucket]
                lo = min(window, key=lambda r: r.temp_c)
                hi = max(window, key=lambda r: r.temp_c)
                kept.extend([lo] if lo is hi else sorted({lo, hi}, key=lambda r: r.timestamp))
            series = [
                {
                    "t": r.timestamp.isoformat(),
                    "c": r.temp_c,
                    "out": r.temp_c < policy.min_c or r.temp_c > policy.max_c,
                }
                for r in kept
            ]
            loggers.append(
                {
                    "logger_id": logger_id,
                    "artifact_id": art.artifact_id,
                    "reading_count": len(readings),
                    "malformed_rows": malformed,
                    "downsampled_to": len(series),
                    "series": series,
                    "metrics": {
                        "peak_c": summary.max_c,
                        "min_c": summary.min_c,
                        "cumulative_minutes_out": summary.minutes_out_of_range,
                        "longest_continuous_minutes": summary.longest_continuous_minutes,
                        "largest_gap_minutes": summary.largest_gap_minutes,
                    },
                    "status": summary.status.value,
                    "reason_codes": [c.value for c in summary.reason_codes],
                    "summary": summary.summary,
                }
            )
        return {
            "case_id": case_id,
            "sample_id": sample_id,
            "permitted": {
                "min_c": policy.min_c,
                "max_c": policy.max_c,
                "tolerance_minutes": policy.tolerance_minutes,
            },
            "loggers": loggers,
        }

    @app.post("/api/cases/{case_id}/samples/{sample_id}/quarantine-review")
    def quarantine_review(
        case_id: str, sample_id: str, body: QuarantineReviewIn, who: ActorContext = Depends(actor)
    ) -> dict[str, Any]:
        """Reopen a hold. This re-verifies the specimen; it does not accept it, the policy engine decides
        again, and may well hold it again."""
        get_case_or_404(case_id)
        with lease(case_id):
            result = svc().open_quarantine_review(
                OpenQuarantineReviewCommand(
                    operation_id=derive_operation_id(
                        case_id=case_id,
                        event_id=f"quarantine-review-{sample_id}",
                        command_type="open_quarantine_review",
                        payload={"sample_id": sample_id, "reason": body.reason},
                    ),
                    case_id=case_id,
                    actor=who,
                    sample_id=sample_id,
                    reason=body.reason,
                )
            )
        return {
            "status": result.status,
            "summary": result.summary,
            "sample": svc().repo.get_sample(sample_id).model_dump(mode="json"),
            "case_state": svc().repo.get_case(case_id).state.value,
        }

    @app.get("/api/cases/{case_id}/verification-report")
    def verification_report(case_id: str, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        """The Shipment Verification Report the receiving lab owes the sending site (ISBER §J6, §L4.5).

        A read over what was already recorded, so it cannot drift from what happened."""
        get_case_or_404(case_id)
        return build_verification_report(svc(), st.ramp, case_id)

    @app.get("/api/cases/{case_id}/decisions")
    def get_decisions(case_id: str, who: ActorContext = Depends(actor)) -> list[dict[str, Any]]:
        get_case_or_404(case_id)
        return [
            p.model_dump(mode="json")
            for p in svc().repo.list_pending_decisions(case_id, unresolved_only=False)
        ]

    @app.post("/api/cases/{case_id}/run")
    def run_case(case_id: str, body: RunRequest, who: ActorContext = Depends(actor)) -> dict[str, Any]:
        case = get_case_or_404(case_id)
        if body.event_type not in (InvocationEventType.CASE_READY, InvocationEventType.RETRY_REQUESTED):
            raise HTTPException(
                status_code=400, detail="use the evidence or decision endpoints for those events"
            )
        event = InvocationEvent(
            case_id=case_id,
            event_id=f"EVT-{case_id}-{body.event_type.value}",
            event_type=body.event_type,
            trusted_actor_id=who.actor_id,
            trusted_actor_role=who.role,
            session_id=case.agent_session_id,
            trace_id=str(uuid.uuid4()),
        )
        return run(event).model_dump(mode="json")

    # -- evidence requests ---------------------------------------------------------------------
    @app.get("/api/evidence-requests/{request_id}")
    def get_request(request_id: str) -> dict[str, Any]:
        try:
            r = svc().repo.get_request(request_id)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        contact = svc().repo.get_contact(r.recipient_contact_id)
        return {
            "request_id": r.request_id,
            "case_id": r.case_id,
            "status": r.status.value,
            "recipient": {
                "contact_id": r.recipient_contact_id,
                "display_name": contact.display_name if contact else None,
            },
            "subject": r.subject,
            "body": r.body,
            "requirements": [q.model_dump(mode="json") for q in r.requirements],
            "affected_sample_ids": list(r.affected_sample_ids),
            "sent_at": r.sent_at.isoformat(),
            "expires_at": r.expires_at.isoformat(),
        }

    @app.post("/api/evidence-requests/{request_id}/complete")
    def complete_request(request_id: str, body: CompleteEvidenceRequest) -> dict[str, Any]:
        try:
            r = svc().repo.get_request(request_id)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        import secrets

        if not secrets.compare_digest(body.upload_token, r.upload_token):
            raise HTTPException(status_code=403, detail="invalid upload token")
        case = get_case_or_404(r.case_id)
        digest = hashlib.sha256((body.model_dump_json()).encode()).hexdigest()[:16]
        event_id = f"EVT-{request_id}-{digest}"
        files = [(f.filename, f.mime_type, base64.b64decode(f.content_base64)) for f in body.files]
        artifacts: tuple[IncomingArtifact, ...] = ()
        refs: tuple[str, ...] = ()
        storage = svc().storage
        if settings.invoker == "agentcore":
            put_staged = getattr(storage, "put_staged", None)
            if put_staged is None:
                raise HTTPException(status_code=500, detail="agentcore invoker requires stageable storage")
            refs = tuple(put_staged(case.case_id, event_id, name, data) for name, _mime, data in files)
            for (name, mime, _data), ref in zip(files, refs, strict=True):
                # record mime alongside the ref via a naming convention understood by the runtime
                _ = (name, mime, ref)
        else:
            artifacts = tuple(IncomingArtifact(filename=n, mime_type=m, content=d) for n, m, d in files)
        delivery = EvidenceDelivery(
            request_id=request_id,
            upload_token=body.upload_token,
            submitted_by_contact_id=body.submitted_by_contact_id,
            artifacts=artifacts,
            artifact_refs=refs,
            sender_message=body.sender_message,
        )
        event = InvocationEvent(
            case_id=case.case_id,
            event_id=event_id,
            event_type=InvocationEventType.EVIDENCE_RECEIVED,
            trusted_actor_id=body.submitted_by_contact_id,
            trusted_actor_role=ActorRole.SITE_CONTACT,
            session_id=case.agent_session_id,
            trace_id=str(uuid.uuid4()),
            evidence=delivery,
        )
        return run(event).model_dump(mode="json")

    # -- human decisions -----------------------------------------------------------------------
    @app.post("/api/cases/{case_id}/interrupts/{interrupt_id}/respond")
    def respond(
        case_id: str, interrupt_id: str, body: DecisionIn, who: ActorContext = Depends(actor)
    ) -> dict[str, Any]:
        case = get_case_or_404(case_id)
        unresolved = svc().repo.list_pending_decisions(case_id)
        pending = [p for p in unresolved if p.interrupt_id == interrupt_id]
        if not pending:
            current = [p.interrupt_id for p in unresolved if p.interrupt_id]
            detail = (
                f"that interrupt id is stale; the open decision is now {current[0]}"
                if current
                else "no pending decision with that interrupt id"
            )
            raise HTTPException(status_code=404, detail=detail)
        issue = pending[0]
        if body.selected_option not in {o.option.value for o in issue.options}:
            raise HTTPException(
                status_code=400, detail=f"option must be one of {[o.option.value for o in issue.options]}"
            )
        event = InvocationEvent(
            case_id=case_id,
            event_id=f"EVT-{issue.issue_id}-{body.selected_option}",
            event_type=InvocationEventType.HUMAN_DECISION_RECEIVED,
            trusted_actor_id=who.actor_id,
            trusted_actor_role=who.role,
            session_id=case.agent_session_id,
            trace_id=str(uuid.uuid4()),
            interrupt_responses=(
                {
                    "interruptId": interrupt_id,
                    "response": {"selected_option": body.selected_option, "comment": body.comment},
                },
            ),
        )
        return run(event).model_dump(mode="json")

    @app.exception_handler(BioIntakeError)
    def domain_error(_request: Any, exc: BioIntakeError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": exc.message, "code": exc.code.value})

    return app


def parse_corrections(items: list[CorrectionIn]) -> tuple[ProposedCorrection, ...]:
    return tuple(ProposedCorrection(**c.model_dump()) for c in items)


def staff_users_spec() -> tuple[str, dict[str, str]]:
    """A BIOINTAKE_USERS value for the default staff, with freshly minted tokens.

    For callers that stand up their own BioIntake and then have to sign in to it: the end-to-end
    script and the live-model harness. It mints the credentials rather than knowing any.
    """
    tokens = {user_id: mint_token() for user_id in DEFAULT_STAFF}
    spec = ";".join(
        f"{user_id}|{display_name}|{role.value}|{tokens[user_id]}"
        for user_id, (display_name, role) in DEFAULT_STAFF.items()
    )
    return spec, tokens


def sign_in(app: FastAPI, user_id: str) -> dict[str, str]:
    """Authorization header for a user whose token this process minted.

    Only works against the in-memory backend, where tokens are generated at start-up: a deployment
    reads them from the environment and this returns nothing it could sign in with.
    """
    token = app.state.biointake.issued_tokens.get(user_id)
    if token is None:
        raise KeyError(f"no locally minted token for {user_id}")
    return {"Authorization": f"Bearer {token}"}
