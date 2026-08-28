"""Trusted, scoped operation ids (Phase 1A.1 §2.4)."""

from __future__ import annotations

import pytest

from biointake.domain.commands import (
    RESERVED_AUTHORITY_FIELDS,
    RequestDispositionCommand,
    canonical_semantic_payload,
    derive_operation_id,
)
from biointake.domain.enums import CaseState, Disposition, ReasonCode
from biointake.domain.errors import DuplicateOperationError
from conftest import AGENT, dispose


def test_derivation_is_deterministic_and_scoped():
    a = derive_operation_id(
        "CASE-1", "EV-1", "RequestDispositionCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )
    b = derive_operation_id(
        "CASE-1", "EV-1", "RequestDispositionCommand", {"requested": "ACCEPT", "sample_id": "BX-209"}
    )
    assert a == b and a.startswith("OP-")
    assert a != derive_operation_id(
        "CASE-2", "EV-1", "RequestDispositionCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )
    assert a != derive_operation_id(
        "CASE-1", "EV-2", "RequestDispositionCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )
    assert a != derive_operation_id(
        "CASE-1", "EV-1", "FinalizeCaseCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )


def test_model_supplied_authority_fields_cannot_influence_the_id():
    base = {"sample_id": "BX-209", "requested": "ACCEPT"}
    injected = {
        **base,
        "operation_id": "OP-0001",
        "actor_role": "PRINCIPAL_INVESTIGATOR",
        "case_id": "CASE-OTHER",
        "nested": {"policy_evaluation_id": "PE-0001", "x": 1},
    }
    assert canonical_semantic_payload(injected) == {
        "nested": {"x": 1},
        "sample_id": "BX-209",
        "requested": "ACCEPT",
    } or canonical_semantic_payload(injected) == canonical_semantic_payload({**base, "nested": {"x": 1}})
    assert derive_operation_id("C", "E", "T", injected) == derive_operation_id(
        "C", "E", "T", {**base, "nested": {"x": 1}}
    )
    assert {
        "operation_id",
        "actor_role",
        "case_id",
        "policy_evaluation_id",
        "destination",
    } <= RESERVED_AUTHORITY_FIELDS


def test_same_id_same_payload_replays(at_checkpoint_1):
    d = at_checkpoint_1
    op = derive_operation_id(
        d.case_id, "EV-1", "RequestDispositionCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )
    r1 = dispose(d, "BX-209", op=op)
    r2 = dispose(d, "BX-209", op=op)
    assert r1 == r2


def test_same_id_modified_payload_rejected(at_checkpoint_1):
    d = at_checkpoint_1
    op = derive_operation_id(d.case_id, "EV-1", "RequestDispositionCommand", {"sample_id": "BX-209"})
    dispose(d, "BX-209", op=op)
    with pytest.raises(DuplicateOperationError) as e:
        dispose(d, "BX-210", op=op)
    assert e.value.code is ReasonCode.DUPLICATE_OPERATION


def test_same_id_under_another_case_rejected(at_checkpoint_1):
    d = at_checkpoint_1
    op = "OP-SHARED"
    dispose(d, "BX-209", op=op)
    with pytest.raises(DuplicateOperationError) as e:
        d.svc.request_disposition(
            RequestDispositionCommand(
                operation_id=op,
                case_id="CASE-OTHER",
                actor=AGENT,
                sample_id="BX-209",
                requested=Disposition.ACCEPT,
            )
        )
    assert e.value.code is ReasonCode.OPERATION_SCOPE_MISMATCH


def test_same_id_under_another_command_type_rejected(at_checkpoint_1):
    d = at_checkpoint_1
    from biointake.domain.commands import FinalizeCaseCommand

    op = "OP-SHARED-2"
    dispose(d, "BX-209", op=op)
    with pytest.raises(DuplicateOperationError) as e:
        d.svc.finalize(FinalizeCaseCommand(operation_id=op, case_id=d.case_id, actor=AGENT))
    assert e.value.code is ReasonCode.OPERATION_SCOPE_MISMATCH


def test_duplicate_event_after_version_advance_replays(at_checkpoint_1):
    d = at_checkpoint_1
    op = derive_operation_id(
        d.case_id, "EV-DUP", "RequestDispositionCommand", {"sample_id": "BX-209", "requested": "ACCEPT"}
    )
    r1 = dispose(d, "BX-209", op=op)
    d.svc.transitions.transition_case(
        d.case_id, CaseState.VERIFYING, AGENT, ReasonCode.EVIDENCE_RECOVERY_IN_PROGRESS
    )
    r2 = d.svc.request_disposition(
        RequestDispositionCommand(
            operation_id=op,
            case_id=d.case_id,
            expected_case_version=d.version(),
            actor=AGENT,
            sample_id="BX-209",
            requested=Disposition.ACCEPT,
        )
    )
    assert r1 == r2
