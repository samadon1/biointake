"""Tool attempts vs. domain effects (Phase 1A.1 §2.6)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from biointake.domain.commands import RaisePendingDecisionCommand
from biointake.domain.enums import AuditEventType, AuditKind
from conftest import AGENT, next_op


def _count(d, event_type, kind=None):
    return len(
        [
            a
            for a in d.repo.list_audit(d.case_id)
            if a.event_type is event_type and (kind is None or a.kind is kind)
        ]
    )


def test_pending_decision_created_exactly_once(at_checkpoint_2):
    d = at_checkpoint_2
    for _ in range(3):
        d.svc.raise_pending_decision(
            RaisePendingDecisionCommand(
                operation_id=next_op(), case_id=d.case_id, actor=AGENT, sample_id="BX-212"
            )
        )
    assert _count(d, AuditEventType.PENDING_DECISION_CREATED, AuditKind.DOMAIN_EFFECT) == 1
    assert len(d.repo.list_pending_decisions(d.case_id, unresolved_only=False)) == 1


def test_human_decision_applied_exactly_once(completed):
    d = completed
    assert _count(d, AuditEventType.HUMAN_DECISION_RECORDED, AuditKind.DOMAIN_EFFECT) == 1
    assert _count(d, AuditEventType.HUMAN_DECISION_APPLIED, AuditKind.DOMAIN_EFFECT) == 1
    assert _count(d, AuditEventType.PENDING_DECISION_CREATED) == 1
    assert len(d.repo.list_decisions(d.case_id)) == 1
    assert _count(d, AuditEventType.LIMS_WRITE) == 12  # 10 accepted + 2 quarantined, one write each
    rep = d.svc.build_report(d.case_id)
    assert rep["audit_counts_by_kind"]["DOMAIN_EFFECT"] > 0


def test_replays_and_rejections_are_tool_attempts_not_domain_effects(at_checkpoint_1):
    d = at_checkpoint_1
    from conftest import dispose

    op = next_op()
    dispose(d, "BX-209", op=op)
    dispose(d, "BX-209", op=op)
    replays = [a for a in d.repo.list_audit(d.case_id) if a.event_type is AuditEventType.OPERATION_REPLAYED]
    assert replays and all(a.kind is AuditKind.TOOL_ATTEMPT for a in replays)


def test_interrupt_spike_reports_two_attempts_one_effect():
    root = Path(__file__).resolve().parents[2]
    spike = root / "spikes" / "interrupt_resume_file.py"
    out1 = subprocess.run(
        [sys.executable, str(spike), "start"], capture_output=True, text=True, check=True, cwd=root
    )
    out2 = subprocess.run(
        [sys.executable, str(spike), "resume"], capture_output=True, text=True, check=True, cwd=root
    )
    assert "stop_reason = interrupt" in out1.stdout
    assert "tool_attempts=2 pending_decision_records=1 decisions_applied=1" in out2.stdout
