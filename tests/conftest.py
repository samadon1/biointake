from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest

from biointake.domain.commands import (
    ApplyInvalidationPlanCommand,
    IncomingArtifact,
    ProposedCorrection,
    ReceiveEvidenceCommand,
    RequestDispositionCommand,
)
from biointake.domain.enums import ActorRole, ActorType, CheckCategory, CheckStatus, Disposition, ReasonCode
from biointake.domain.models import ActorContext, CheckResult, CommandResult
from biointake.domain.policies import default_policy
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package
from run_deterministic_demo import Demo

FIXTURE_DIR: Path = DEFAULT_FIXTURE_DIR
NOW = datetime(2026, 8, 26, 16, 0, tzinfo=UTC)
AGENT = ActorContext.agent("test-agent")
COORDINATOR = ActorContext(actor_type=ActorType.HUMAN, actor_id="coordinator-1", role=ActorRole.COORDINATOR)
PI = ActorContext(actor_type=ActorType.HUMAN, actor_id="pi-1", role=ActorRole.PRINCIPAL_INVESTIGATOR)
SENDER = ActorContext(actor_type=ActorType.SENDER, actor_id="SITE-CONTACT-002", role=ActorRole.SITE_CONTACT)

_ops = itertools.count(1000)


def next_op() -> str:
    return f"T-OP-{next(_ops):05d}"


@pytest.fixture(scope="session")
def package():
    return load_package(FIXTURE_DIR)


@pytest.fixture(scope="session")
def policy():
    return default_policy()


@pytest.fixture
def demo() -> Demo:
    return Demo(FIXTURE_DIR, verbose=False)


@pytest.fixture
def at_checkpoint_1(demo: Demo) -> Demo:
    demo.stage_1_initial()
    return demo


@pytest.fixture
def at_checkpoint_2(at_checkpoint_1: Demo) -> Demo:
    at_checkpoint_1.stage_2_evidence()
    return at_checkpoint_1


@pytest.fixture
def completed(at_checkpoint_2: Demo) -> Demo:
    at_checkpoint_2.stage_3_human()
    return at_checkpoint_2


# ---------------------------------------------------------------------------------------------
def make_check(
    sample_id: str,
    category: CheckCategory,
    status: CheckStatus,
    codes: tuple[ReasonCode, ...] = (),
    case_id: str = "CASE-T",
) -> CheckResult:
    return CheckResult(
        check_id=f"CHK-{sample_id}-{category.value}",
        case_id=case_id,
        sample_id=sample_id,
        category=category,
        status=status,
        reason_codes=codes,
        evidence_refs=("ART-x",),
        rule_version="POLICY-PROTO-042@3.0.0",
        evaluator="test",
        evaluated_at=NOW,
    )


def all_pass(sample_id: str = "S1") -> dict[CheckCategory, CheckResult]:
    return {c: make_check(sample_id, c, CheckStatus.PASS) for c in CheckCategory}


def dispose(
    demo: Demo, sample_id: str, requested: Disposition = Disposition.ACCEPT, op: str | None = None, **kw
) -> CommandResult:
    return demo.svc.request_disposition(
        RequestDispositionCommand(
            operation_id=op or next_op(),
            case_id=demo.case_id,
            expected_case_version=demo.version(),
            actor=AGENT,
            sample_id=sample_id,
            requested=requested,
            **kw,
        )
    )


def addendum_artifact(
    package, content: bytes | None = None, declared_sha256: str | None = None
) -> IncomingArtifact:
    return IncomingArtifact(
        filename="consent-addendum.json",
        mime_type="application/json",
        content=content if content is not None else package.later["consent-addendum.json"],
        declared_sha256=declared_sha256,
    )


def row7_correction() -> ProposedCorrection:
    return ProposedCorrection(
        manifest_row=7, manifest_value="BX-2O7", corrected_value="BX-207", sender_statement="typo"
    )


def receive(
    demo: Demo,
    *,
    artifacts: tuple[IncomingArtifact, ...] = (),
    corrections: tuple[ProposedCorrection, ...] = (),
    contact: str = "SITE-CONTACT-002",
    token: str | None = None,
    op: str | None = None,
    request_id: str | None = None,
) -> CommandResult:
    rid = request_id or demo.request_id
    req = demo.repo.get_request(rid)
    return demo.svc.receive_evidence(
        ReceiveEvidenceCommand(
            operation_id=op or next_op(),
            case_id=demo.case_id,
            expected_case_version=demo.version(),
            actor=ActorContext(actor_type=ActorType.SENDER, actor_id=contact, role=ActorRole.SITE_CONTACT),
            request_id=rid,
            upload_token=token if token is not None else req.upload_token,
            submitted_by_contact_id=contact,
            artifacts=artifacts,
            proposed_corrections=corrections,
        )
    )


def apply_plan(demo: Demo, res: CommandResult, op: str | None = None) -> CommandResult:
    """Apply the deterministic invalidation plan produced by a receive_evidence result."""
    return demo.svc.apply_invalidation_plan(
        ApplyInvalidationPlanCommand(
            operation_id=op or next_op(),
            case_id=demo.case_id,
            expected_case_version=demo.version(),
            actor=AGENT,
            plan_id=res.data["plan_id"],
        )
    )
