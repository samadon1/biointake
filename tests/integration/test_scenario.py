"""The canonical SHIP-DEMO-001 trajectory, reproducibility, and prompt-injection safety."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from biointake.domain.enums import ArtifactType, CaseState, SampleState
from conftest import FIXTURE_DIR
from generate_fixture import INJECTION_TEXT, build
from run_deterministic_demo import Demo, verify_against_expected


def test_three_checkpoints_match_expected(demo: Demo):
    checkpoints = demo.run()
    assert verify_against_expected(checkpoints, FIXTURE_DIR) == []
    assert checkpoints["checkpoint_1"]["case_state"] == CaseState.WAITING_FOR_EVIDENCE.value
    assert checkpoints["checkpoint_2"]["case_state"] == CaseState.NEEDS_HUMAN_DECISION.value
    assert checkpoints["checkpoint_2"]["checks_rerun"] == 4
    assert checkpoints["checkpoint_3"]["case_state"] == CaseState.COMPLETED.value


def test_final_counts_and_zero_unauthorized_acceptances(completed: Demo):
    report = completed.svc.build_report(completed.case_id)
    expected = json.loads((FIXTURE_DIR / "expected" / "final-state.json").read_text())
    assert report["counts"]["ACCEPTED"] == expected["accepted"] == 10
    assert report["counts"]["QUARANTINED"] == expected["quarantined"] == 2
    assert report["counts"]["ACCEPTED_WITH_EXCEPTION"] == 0
    assert report["unauthorized_acceptances"] == 0
    assert len(report["evidence_requests"]) == 1 and report["evidence_requests"][0]["status"] == "SATISFIED"
    assert len(report["human_decisions"]) == 1
    # every accepted sample is backed by an ALLOWED evaluation recorded in the LIMS
    for s in report["samples"]:
        if s["state"] == "ACCEPTED":
            assert s["lims"]["status"] == "ACCEPTED" and s["lims"]["policy_evaluation_id"]
            assert all(v == "PASS" for v in s["checks"].values())
    quarantined = {s["sample_id"]: s for s in report["samples"] if s["state"] == "QUARANTINED"}
    assert set(quarantined) == {"BX-211", "BX-212"}
    assert quarantined["BX-211"]["checks"]["LIMS_RECONCILIATION"] == "FAIL"
    assert quarantined["BX-212"]["checks"]["TEMPERATURE_REQUIREMENT"] == "FAIL"


def test_lims_history_record_untouched_after_run(completed: Demo):
    hist = completed.svc.lims.get("LIMS-HIST-0093")
    assert (
        hist is not None and hist.status == "ARCHIVED" and hist.sample_id == "AR-0093" and hist.history == ()
    )


def test_audit_trail_is_ordered_and_complete(completed: Demo):
    events = completed.repo.list_audit(completed.case_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    types = [e.event_type.value for e in events]
    for required in (
        "CASE_CREATED",
        "EVIDENCE_REQUEST_SENT",
        "EVIDENCE_RECEIVED",
        "EVIDENCE_REQUEST_SATISFIED",
        "PENDING_DECISION_CREATED",
        "HUMAN_DECISION_RECORDED",
        "CASE_FINALIZED",
    ):
        assert required in types
    # evidence request precedes the human decision request (ADR 0002)
    assert types.index("EVIDENCE_REQUEST_SATISFIED") < types.index("PENDING_DECISION_CREATED")
    assert types[-1] == "CASE_FINALIZED"
    report_art = [a for a in completed.repo.list_artifacts(completed.case_id, ArtifactType.AUDIT_REPORT)]
    assert len(report_art) == 1


def test_fixture_generation_is_deterministic(tmp_path: Path):
    committed = json.loads((FIXTURE_DIR / "fixture-manifest.json").read_text())["files"]
    regenerated = build(tmp_path)
    assert regenerated == committed
    for rel, digest in committed.items():
        assert hashlib.sha256((FIXTURE_DIR / rel).read_bytes()).hexdigest() == digest


def test_prompt_injection_in_addendum_changes_nothing(completed: Demo):
    addenda = completed.repo.list_artifacts(completed.case_id, ArtifactType.CONSENT_ADDENDUM)
    assert len(addenda) == 1
    assert INJECTION_TEXT in str(addenda[0].metadata["untrusted_text"])  # stored as data …
    report = completed.svc.build_report(completed.case_id)
    assert (
        report["counts"]["ACCEPTED"] == 10 and report["counts"]["QUARANTINED"] == 2
    )  # … and changed nothing
    assert completed.svc.policy == completed.package.policy
    assert completed.repo.get_sample("BX-212").state is SampleState.QUARANTINED


def test_snapshot_is_compact_and_authoritative(at_checkpoint_1: Demo):
    snap = at_checkpoint_1.svc.snapshot(at_checkpoint_1.case_id)
    assert snap["state"] == "WAITING_FOR_EVIDENCE" and len(snap["active_requests"]) == 1
    by_id = {s["sample_id"]: s for s in snap["samples"]}
    assert by_id["BX-207"]["blockers"][0]["reason_codes"] == ["MANIFEST_IDENTIFIER_NEAR_MATCH"]
    assert by_id["BX-201"]["blockers"] == []
