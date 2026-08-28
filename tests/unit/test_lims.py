from __future__ import annotations

import pytest

from biointake.domain.enums import CheckStatus, Disposition, PolicyDecision, ReasonCode
from biointake.domain.errors import LimsWriteRefusedError
from biointake.domain.models import PolicyEvaluation, Sample
from biointake.services.lims_demo import DemoLims
from conftest import NOW


def sample(sid: str, barcode: str) -> Sample:
    return Sample(
        sample_id=sid,
        case_id="C",
        barcode=barcode,
        specimen_type="PLASMA",
        container_id="BOX-1",
        expected_protocol_id="PROTO-042",
        updated_at=NOW,
    )


def evaluation(sid: str, decision: PolicyDecision, requested=Disposition.ACCEPT) -> PolicyEvaluation:
    return PolicyEvaluation(
        evaluation_id="PE-1",
        policy_id="P",
        policy_version="1",
        case_id="C",
        sample_id=sid,
        requested_disposition=requested,
        decision=decision,
        evaluated_at=NOW,
    )


def test_reconcile_detects_collision_and_keeps_both_records(package):
    lims = DemoLims(package.lims_records)
    r = lims.reconcile(sample("BX-211", "NS042-000211"), "PROTO-042")
    assert r.status is CheckStatus.FAIL and r.reason_codes == (ReasonCode.BARCODE_COLLISION,)
    assert {"LIMS-HIST-0093", "LIMS-EXP-0211"} <= set(r.record_ids)
    assert lims.reconcile(sample("BX-201", "NS042-000201"), "PROTO-042").status is CheckStatus.PASS


def test_write_refused_without_stored_allowed_evaluation(package):
    lims = DemoLims(package.lims_records)
    s = sample("BX-201", "NS042-000201")
    ev = evaluation("BX-201", PolicyDecision.ALLOWED)
    with pytest.raises(LimsWriteRefusedError):
        lims.write_disposition(s, ev, "op", lambda _id: None)  # not on record
    with pytest.raises(LimsWriteRefusedError):
        lims.write_disposition(
            s,
            evaluation("BX-201", PolicyDecision.DENIED),
            "op",
            lambda _id: evaluation("BX-201", PolicyDecision.DENIED),
        )
    with pytest.raises(LimsWriteRefusedError):
        lims.write_disposition(
            s, ev, "op", lambda _id: evaluation("BX-201", PolicyDecision.ALLOWED, Disposition.QUARANTINE)
        )  # differs
    assert lims.write_count == 0


def test_write_is_idempotent_on_operation_id(package):
    lims = DemoLims(package.lims_records)
    s = sample("BX-201", "NS042-000201")
    ev = evaluation("BX-201", PolicyDecision.ALLOWED)
    a = lims.write_disposition(s, ev, "op-1", lambda _id: ev)
    b = lims.write_disposition(s, ev, "op-1", lambda _id: ev)
    assert a == b and lims.write_count == 1 and a.status == "ACCEPTED"


def test_colliding_identity_is_never_overwritten(package):
    lims = DemoLims(package.lims_records)
    s = sample("BX-211", "NS042-000211")
    hist_before = lims.get("LIMS-HIST-0093")
    with pytest.raises(LimsWriteRefusedError):
        lims.write_disposition(
            s,
            evaluation("BX-211", PolicyDecision.ALLOWED),
            "op",
            lambda _id: evaluation("BX-211", PolicyDecision.ALLOWED),
        )
    q = evaluation("BX-211", PolicyDecision.ALLOWED, Disposition.QUARANTINE)
    rec = lims.write_disposition(s, q, "op-q", lambda _id: q)
    assert rec.record_id == "LIMS-EXP-0211" and rec.status == "QUARANTINED"
    assert lims.get("LIMS-HIST-0093") == hist_before  # untouched
