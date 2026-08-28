from __future__ import annotations

import pytest

from biointake.domain.commands import FinalizeCaseCommand
from biointake.domain.enums import AuditEventType, Disposition, ReasonCode
from biointake.domain.errors import DuplicateOperationError, VersionConflictError
from conftest import AGENT, dispose, next_op


def test_duplicate_disposition_same_payload_is_one_lims_write(demo):
    demo.stage_1_initial()
    from biointake.domain.commands import RequestDispositionCommand

    op = next_op()
    before = demo.svc.lims.write_count
    res1 = dispose(demo, "BX-209", Disposition.ACCEPT, op=op)
    # a duplicate delivery of the same event may carry a newer case version; it must still replay
    res2 = demo.svc.request_disposition(
        RequestDispositionCommand(
            operation_id=op,
            case_id=demo.case_id,
            expected_case_version=demo.version(),
            actor=AGENT,
            sample_id="BX-209",
            requested=Disposition.ACCEPT,
        )
    )
    assert res1 == res2
    assert demo.svc.lims.write_count == before
    replays = [
        a for a in demo.repo.list_audit(demo.case_id) if a.event_type is AuditEventType.OPERATION_REPLAYED
    ]
    assert len(replays) == 1


def test_accepted_sample_replay_does_not_write_twice(demo):
    demo.stage_1_initial()
    from biointake.domain.commands import RequestDispositionCommand

    op = "T-OP-REPLAY"
    # first execution happened in stage 1 via OP-0001 for BX-201; run a fresh op then replay it exactly
    cmd = RequestDispositionCommand(
        operation_id=op,
        case_id=demo.case_id,
        expected_case_version=demo.version(),
        actor=AGENT,
        sample_id="BX-201",
        requested=Disposition.ACCEPT,
    )
    first = demo.svc.request_disposition(cmd)
    writes = demo.svc.lims.write_count
    second = demo.svc.request_disposition(cmd)
    assert first == second
    assert demo.svc.lims.write_count == writes


def test_reused_operation_id_with_changed_payload_is_rejected(demo):
    demo.stage_1_initial()
    from biointake.domain.commands import RequestDispositionCommand

    op = "T-OP-CHANGED"
    demo.svc.request_disposition(
        RequestDispositionCommand(
            operation_id=op,
            case_id=demo.case_id,
            actor=AGENT,
            sample_id="BX-209",
            requested=Disposition.ACCEPT,
        )
    )
    with pytest.raises(DuplicateOperationError):
        demo.svc.request_disposition(
            RequestDispositionCommand(
                operation_id=op,
                case_id=demo.case_id,
                actor=AGENT,
                sample_id="BX-210",
                requested=Disposition.ACCEPT,
            )
        )
    rejected = [
        a for a in demo.repo.list_audit(demo.case_id) if a.event_type is AuditEventType.OPERATION_REJECTED
    ]
    assert rejected and ReasonCode.DUPLICATE_OPERATION in rejected[-1].reason_codes


def test_stale_case_version_is_rejected(demo):
    demo.stage_1_initial()
    from biointake.domain.commands import RequestDispositionCommand

    with pytest.raises(VersionConflictError):
        demo.svc.request_disposition(
            RequestDispositionCommand(
                operation_id=next_op(),
                case_id=demo.case_id,
                expected_case_version=demo.version() - 1,
                actor=AGENT,
                sample_id="BX-209",
                requested=Disposition.ACCEPT,
            )
        )


def test_failed_command_is_not_recorded_so_retry_can_succeed(demo):
    demo.stage_1_initial()
    op = next_op()
    from biointake.domain.errors import PolicyDeniedError

    with pytest.raises(PolicyDeniedError):
        demo.svc.finalize(FinalizeCaseCommand(operation_id=op, case_id=demo.case_id, actor=AGENT))
    assert demo.repo.get_operation(op) is None
