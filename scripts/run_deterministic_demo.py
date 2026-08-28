"""Deterministic reference trajectory for SHIP-DEMO-001, NO LLM involved.

This is the controller the future Strands agent must match. Where the agent will interpret free
text (the manifest note, the sender's reply), this script injects the *structured* equivalent from
the fixture and says so. It never pretends to understand prose.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from biointake.clock import SteppingClock  # noqa: E402
from biointake.domain.commands import (  # noqa: E402
    ApplyInvalidationPlanCommand,
    CreateEvidenceRequestCommand,
    FinalizeCaseCommand,
    IncomingArtifact,
    ProposedCorrection,
    RaisePendingDecisionCommand,
    ReceiveEvidenceCommand,
    RecordHumanDecisionCommand,
    RequestDispositionCommand,
)
from biointake.domain.enums import (  # noqa: E402
    ActorRole,
    ActorType,
    Disposition,
    EvidenceRequestStatus,
    HumanOption,
    ReasonCode,
    SampleState,
)
from biointake.domain.models import ActorContext, EvidenceRequirement  # noqa: E402
from biointake.fixtures import DEFAULT_FIXTURE_DIR, load_package  # noqa: E402
from biointake.repositories.memory import InMemoryRepository  # noqa: E402
from biointake.services.intake import IntakeService  # noqa: E402
from biointake.storage.local import MemoryArtifactStorage  # noqa: E402

CONTROLLER = ActorContext.agent("deterministic-controller")
COORDINATOR = ActorContext(
    actor_type=ActorType.HUMAN, actor_id="coordinator-ama-asante", role=ActorRole.COORDINATOR
)


class Demo:
    def __init__(self, fixture_dir: Path = DEFAULT_FIXTURE_DIR, verbose: bool = True) -> None:
        self.package = load_package(fixture_dir)
        self.verbose = verbose
        self.clock = SteppingClock(datetime(2026, 8, 26, 16, 0, tzinfo=UTC), step_seconds=1)
        self.repo = InMemoryRepository(self.clock)
        self.svc = IntakeService(
            self.repo,
            MemoryArtifactStorage(),
            self.package.policy,
            self.clock,
            token_factory=lambda: "demo-upload-token-0001",
        )
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self._op = 0

    # ------------------------------------------------------------------------------------------
    def op(self) -> str:
        self._op += 1
        return f"OP-{self._op:04d}"

    def say(self, msg: str = "") -> None:
        if self.verbose:
            print(msg)

    def version(self) -> int:
        return self.repo.get_case(self.case_id).case_version

    # ------------------------------------------------------------------------------------------
    def run(self) -> dict[str, dict[str, Any]]:
        self.stage_1_initial()
        self.stage_2_evidence()
        self.stage_3_human()
        return self.checkpoints

    def stage_1_initial(self) -> None:
        self.say("=" * 78)
        self.say("STAGE 1, shipment arrives; verify everything available")
        self.say("=" * 78)
        case = self.svc.create_case(self.package, ActorContext.system())
        self.case_id = case.case_id
        self.svc.begin_verification(self.case_id, CONTROLLER)
        checks = self.svc.verify(self.case_id, CONTROLLER)
        self.say(f"Ran {len(checks)} checks on {len(self.repo.list_samples(self.case_id))} samples.")

        requirements: list[EvidenceRequirement] = []
        for sample in self.repo.list_samples(self.case_id):
            res = self.svc.request_disposition(
                RequestDispositionCommand(
                    operation_id=self.op(),
                    case_id=self.case_id,
                    expected_case_version=self.version(),
                    actor=CONTROLLER,
                    sample_id=sample.sample_id,
                    requested=Disposition.ACCEPT,
                )
            )
            if res.status == "denied" and ReasonCode.BARCODE_COLLISION in res.reason_codes:
                res = self.svc.request_disposition(
                    RequestDispositionCommand(
                        operation_id=self.op(),
                        case_id=self.case_id,
                        expected_case_version=self.version(),
                        actor=CONTROLLER,
                        sample_id=sample.sample_id,
                        requested=Disposition.QUARANTINE,
                        reason_code=ReasonCode.BARCODE_COLLISION,
                    )
                )
            if res.status == "waiting":
                requirements.extend(
                    EvidenceRequirement.model_validate(r) for r in res.data["recoverable_requirements"]
                )
            self.say(f"  {sample.sample_id}: {res.status:<14} {res.summary}")

        # The agent will pick the recipient by reading the manifest note ("K. Mensah") and searching the
        # verified contact directory. The deterministic controller injects that structured choice.
        recipient = "SITE-CONTACT-002"
        res = self.svc.create_evidence_request(
            CreateEvidenceRequestCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=CONTROLLER,
                recipient_contact_id=recipient,
                requirements=tuple(requirements),
            )
        )
        self.request_id = res.data["request_id"]
        self.say(f"\nOne consolidated evidence request → {recipient}: {res.data['request_id']}")
        self.say("-" * 78)
        self.say(res.data["body"])
        self.say("-" * 78)
        self.svc.recompute_case_state(self.case_id, CONTROLLER)
        self.checkpoint(
            "checkpoint_1", extra={"evidence_requests": len(self.repo.list_requests(self.case_id))}
        )

    def stage_2_evidence(self) -> None:
        self.say("\n" + "=" * 78)
        self.say("STAGE 2, sender replies (structured equivalent of the free-text reply is injected)")
        self.say("=" * 78)
        reply = json.loads(self.package.later["sender-reply.json"])
        self.say(f"Sender free text (NOT interpreted by this script): {reply['free_text']!r}")
        corrections = tuple(
            ProposedCorrection.model_validate(c)
            for c in reply["structured_equivalent"]["proposed_corrections"]
        )
        request = self.repo.get_request(self.request_id)  # server-side read of the upload token
        res = self.svc.receive_evidence(
            ReceiveEvidenceCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=ActorContext(
                    actor_type=ActorType.SENDER,
                    actor_id=reply["from_contact_id"],
                    role=ActorRole.SITE_CONTACT,
                ),
                request_id=self.request_id,
                upload_token=request.upload_token,
                submitted_by_contact_id=reply["from_contact_id"],
                artifacts=(
                    IncomingArtifact(
                        filename="consent-addendum.json",
                        mime_type="application/json",
                        content=self.package.later["consent-addendum.json"],
                    ),
                ),
                proposed_corrections=corrections,
            )
        )
        self.say(f"Evidence receipt: {res.summary}")
        # The deterministic dependency service, not this script, not the future agent, decided which
        # checks the new evidence invalidates. Apply the stored plan by id.
        plan = self.repo.get_plan(res.data["plan_id"])
        assert plan is not None
        self.say("Invalidation plan " + plan.plan_id + ":")
        for cid, why in plan.reasons_by_check.items():
            self.say(f"  - {cid}: {why}")
        applied = self.svc.apply_invalidation_plan(
            ApplyInvalidationPlanCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=CONTROLLER,
                plan_id=plan.plan_id,
            )
        )
        self.checks_rerun = len(applied.data["produced_check_ids"])
        total = applied.data["total_check_slots"]
        self.say(
            f"Re-ran {self.checks_rerun} of {total} checks: "
            + ", ".join(f"{r['sample_id']}:{r['category']}={r['status']}" for r in applied.data["reverified"])
        )
        affected_samples = list(dict.fromkeys(r["sample_id"] for r in applied.data["reverified"]))
        for sid in affected_samples:
            r = self.svc.request_disposition(
                RequestDispositionCommand(
                    operation_id=self.op(),
                    case_id=self.case_id,
                    expected_case_version=self.version(),
                    actor=CONTROLLER,
                    sample_id=sid,
                    requested=Disposition.ACCEPT,
                )
            )
            self.say(f"  {sid}: {r.status:<14} {r.summary}")
        assert not self.repo.list_requests(self.case_id, EvidenceRequestStatus.ACTIVE)
        # Now, and only now, the human decision for BX-212 may be raised.
        r = self.svc.raise_pending_decision(
            RaisePendingDecisionCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=CONTROLLER,
                sample_id="BX-212",
            )
        )
        self.issue_id = r.data["issue_id"]
        card = r.data["card"]
        self.say("\nDecision card raised:")
        self.say(f"  Sample {card['sample_id']}, {card['issue_type']}")
        self.say(f"  Observed: {card['observed_value']}")
        self.say(f"  Expected: {card['expected_value']}")
        self.say(f"  Verified: {', '.join(card['passed_checks'])}")
        self.say(f"  Blocked:  {', '.join(card['blocked_checks'])}")
        self.say(f"  Policy:   {card['policy_clause']}")
        self.say(
            "  Options:  "
            + " | ".join(f"{o['option']} (roles: {', '.join(o['required_roles'])})" for o in card["options"])
        )
        self.svc.recompute_case_state(self.case_id, CONTROLLER)
        self.checkpoint("checkpoint_2", extra={"checks_rerun": self.checks_rerun, "total_check_slots": total})

    def stage_3_human(self) -> None:
        self.say("\n" + "=" * 78)
        self.say("STAGE 3, authorized human chooses QUARANTINE (client-supplied role is ignored)")
        self.say("=" * 78)
        res = self.svc.record_human_decision(
            RecordHumanDecisionCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=COORDINATOR,  # trusted server context
                issue_id=self.issue_id,
                selected_option=HumanOption.QUARANTINE,
                comment="19-minute excursion; hold pending PI review of downstream use.",
                client_payload={"actor_role": "PRINCIPAL_INVESTIGATOR"},  # untrusted; ignored
            )
        )
        self.say(f"Decision: {res.summary}")
        fin = self.svc.finalize(
            FinalizeCaseCommand(
                operation_id=self.op(),
                case_id=self.case_id,
                expected_case_version=self.version(),
                actor=CONTROLLER,
            )
        )
        self.say(f"Finalized: {fin.summary}")
        self.checkpoint(
            "checkpoint_3", extra={"human_decisions": len(self.repo.list_decisions(self.case_id))}
        )
        report = self.svc.build_report(self.case_id)
        self.say("\nAudit trail:")
        for ev in report["audit_events"]:
            self.say(f"  {ev['seq']:>3}  {ev['kind'][:6]:<6} {ev['type']:<28} {ev['summary']}")
        self.say(f"\naudit counts by kind = {report['audit_counts_by_kind']}")
        self.say(f"\nunauthorized_acceptances = {report['unauthorized_acceptances']}")

    # ------------------------------------------------------------------------------------------
    def checkpoint(self, name: str, extra: dict[str, Any]) -> None:
        case = self.repo.get_case(self.case_id)
        by_state: dict[str, list[str]] = {}
        for s in self.repo.list_samples(self.case_id):
            by_state.setdefault(s.state.value, []).append(s.sample_id)
        snap: dict[str, Any] = {
            "case_state": case.state.value,
            **{k: sorted(v) for k, v in by_state.items()},
            **extra,
        }
        self.checkpoints[name] = snap
        self.say(f"\n>>> {name.upper()}  case state: {case.state.value}")
        for state in SampleState:
            if state.value in by_state:
                self.say(f"    {state.value:<22} {', '.join(sorted(by_state[state.value]))}")
        for k, v in extra.items():
            self.say(f"    {k:<22} {v}")


def verify_against_expected(checkpoints: dict[str, dict[str, Any]], fixture_dir: Path) -> list[str]:
    expected = json.loads((fixture_dir / "expected" / "checkpoints.json").read_text())
    problems = []
    for name, exp in expected.items():
        got = checkpoints.get(name, {})
        for key, val in exp.items():
            if got.get(key) != val:
                problems.append(f"{name}.{key}: expected {val!r}, got {got.get(key)!r}")
    return problems


def main() -> int:
    fixture_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE_DIR
    demo = Demo(fixture_dir)
    checkpoints = demo.run()
    problems = verify_against_expected(checkpoints, fixture_dir)
    print()
    if problems:
        print("MISMATCH against fixtures/expected/checkpoints.json:")
        for p in problems:
            print("  - " + p)
        return 1
    print("All three checkpoints match fixtures/expected/checkpoints.json ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
