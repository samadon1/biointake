# ADR 0002, One unresolved interaction stage at a time

**Status:** accepted (Phase 1A) · **Date:** 2026-08-27

## Context

In SHIP-DEMO-001 two different kinds of blocker coexist: recoverable missing evidence
(BX-207, BX-209, BX-210) and a decision only a human may take (BX-212). Left unordered, the case
could be simultaneously "waiting for the sender" and "waiting for the coordinator", with two
pending interrupts, two resume paths, and a confusing story.

## Decision

The case-level stable state is computed by `IntakeService.recompute_case_state` with a fixed
priority:

```
FAILED  >  WAITING_FOR_EVIDENCE  >  NEEDS_HUMAN_DECISION  >  (VERIFYING / COMPLETED)
```

- Evidence recovery precedes human interruption. `raise_pending_decision` refuses
  (`EVIDENCE_RECOVERY_IN_PROGRESS`) while any evidence request is `ACTIVE`. The human is asked only
  once the machine has exhausted what it can recover on its own, so the decision card is complete
  and is never superseded by evidence that arrives a minute later.
- The MVP supports exactly one unresolved interaction stage per case at a time. Per-sample blockers
  remain fully independent (`Sample.state` and each `CheckResult`); the priority only governs which
  *external party* the case is currently waiting on.
- A `Sample` may sit in `NEEDS_HUMAN_DECISION` while the case is `WAITING_FOR_EVIDENCE`; the
  decision card (`PendingDecision`) is created later, idempotently keyed on `issue_id`.

## Consequences

- The canonical trajectory is linear and easy to persist, replay and film:
  `VERIFYING → WAITING_FOR_EVIDENCE → VERIFYING → NEEDS_HUMAN_DECISION → VERIFYING → COMPLETED`.
- There is at most one Strands interrupt outstanding per case, which keeps session resume simple.
- Multiple missing-evidence blockers are still consolidated into one request (fingerprinted so the
  same requirement set cannot be requested twice while active).

## Notes for Phase 2

- The Strands agent must use **sequential tool execution** for the MVP (no concurrent mutating tools).
- Automatic retries are limited to read-only or explicitly idempotent operations.
- There is no fixed requirement of exactly nine tools; the tool surface is the smallest coherent one.
