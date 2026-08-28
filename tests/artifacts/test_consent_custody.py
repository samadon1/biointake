from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from biointake.domain.enums import CheckStatus, ReasonCode
from biointake.services.consent import (
    evaluate_consent,
    evaluate_protocol_eligibility,
    parse_consent_addendum,
    parse_consent_records,
)
from biointake.services.custody import CustodyEvent, evaluate_custody

T = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def test_registry_v3_passes(package, policy):
    records = parse_consent_records(package.consent_records_json)
    o = evaluate_consent("NS-P-0201", "PROTO-042", records, [], policy.consent, "ART-REG")
    assert o.status is CheckStatus.PASS


def test_v2_without_addendum_is_unavailable_not_fail(package, policy):
    records = parse_consent_records(package.consent_records_json)
    o = evaluate_consent("NS-P-0209", "PROTO-042", records, [], policy.consent, "ART-REG")
    assert o.status is CheckStatus.UNAVAILABLE and o.reason_codes == (ReasonCode.CONSENT_ADDENDUM_MISSING,)


def test_addendum_covers_only_listed_participants(package, policy):
    records = parse_consent_records(package.consent_records_json)
    add = parse_consent_addendum(package.later["consent-addendum.json"])
    ok = evaluate_consent("NS-P-0209", "PROTO-042", records, [("ART-ADD", add)], policy.consent, "ART-REG")
    assert ok.status is CheckStatus.PASS and "ART-ADD" in ok.evidence_refs
    other = json.loads(package.later["consent-addendum.json"])
    other["participants"] = ["NS-P-0299"]
    add2 = parse_consent_addendum(json.dumps(other).encode())
    still = evaluate_consent(
        "NS-P-0209", "PROTO-042", records, [("ART-ADD2", add2)], policy.consent, "ART-REG"
    )
    assert still.status is CheckStatus.UNAVAILABLE


def test_withdrawn_consent_fails(package, policy):
    records = parse_consent_records(package.consent_records_json)
    records = [
        r.model_copy(update={"status": "WITHDRAWN"}) if r.participant_reference == "NS-P-0201" else r
        for r in records
    ]
    o = evaluate_consent("NS-P-0201", "PROTO-042", records, [], policy.consent, "ART-REG")
    assert o.status is CheckStatus.FAIL and o.reason_codes == (ReasonCode.CONSENT_INVALID,)


def test_unknown_participant_is_unavailable(package, policy):
    records = parse_consent_records(package.consent_records_json)
    assert (
        evaluate_consent(None, "PROTO-042", records, [], policy.consent, "R").status
        is CheckStatus.UNAVAILABLE
    )
    assert evaluate_consent("NOPE", "PROTO-042", records, [], policy.consent, "R").reason_codes == (
        ReasonCode.CONSENT_RECORD_MISSING,
    )


def test_protocol_eligibility(policy):
    assert evaluate_protocol_eligibility("PROTO-042", "plasma", policy, "P").status is CheckStatus.PASS
    bad = evaluate_protocol_eligibility("PROTO-017", "SERUM", policy, "P")
    assert bad.status is CheckStatus.FAIL and bad.reason_codes == (ReasonCode.PROTOCOL_MISMATCH,)


def _events(order: list[str]) -> list[CustodyEvent]:
    return [
        CustodyEvent(sample_id="BX-1", event=ev, actor_id=f"A{i}", timestamp=T + timedelta(hours=i))
        for i, ev in enumerate(order)
    ]


def test_custody_complete_and_ordered_passes(policy):
    assert (
        evaluate_custody(
            "BX-1", _events(["COLLECTED", "PACKED", "SHIPPED", "RECEIVED"]), policy.custody
        ).status
        is CheckStatus.PASS
    )


def test_custody_missing_event_is_unavailable(policy):
    o = evaluate_custody("BX-1", _events(["COLLECTED", "SHIPPED", "RECEIVED"]), policy.custody)
    assert o.status is CheckStatus.UNAVAILABLE and o.reason_codes == (ReasonCode.CUSTODY_EVENT_MISSING,)


def test_custody_out_of_order_fails(policy):
    o = evaluate_custody("BX-1", _events(["PACKED", "COLLECTED", "SHIPPED", "RECEIVED"]), policy.custody)
    assert o.status is CheckStatus.FAIL and o.reason_codes == (ReasonCode.CUSTODY_ORDER_INVALID,)
