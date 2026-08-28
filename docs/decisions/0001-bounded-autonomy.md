# ADR 0001, Bounded autonomy: flexible recovery, deterministic acceptance

**Status:** accepted (Phase 1A) · **Date:** 2026-08-27

## Context

BioIntake's agent will read messy human artifacts (manifest notes, a sender's free-text reply),
decide what to inspect next, consolidate blockers into one evidence request, and escalate to a
human. The single biggest real-world risk is a sample being accepted, attached to a participant,
entered into the lab's record system, on the strength of something a model *believed*.

## Decision

1. **Agent reasoning is flexible; acceptance authorization is deterministic.** The Strands agent
   chooses tools, recipients (from a verified directory), request wording, and re-verification
   scope. Whether a disposition is *permitted* is decided only by `domain/disposition.py`
   (`DispositionEngine`), a pure function of the versioned policy, the recorded `CheckResult`s and
   an optional `HumanDecision`.
2. **Required evidence that is unavailable fails closed.** A required check in `FAIL`,
   `UNAVAILABLE`, `AMBIGUOUS` or `ERROR`, or simply missing, blocks ordinary acceptance
   (`REQUIRED_CHECK_MISSING`). Unknown statuses are denied. `tests/test_invariant_fuzz.py` drives
   10,000 random status vectors through the engine and asserts `ALLOWED` never appears unless every
   required check is `PASS`.
3. **The model cannot emit an authoritative `ALLOWED`.** `PolicyDecision` values are produced only
   by the engine and persisted as `PolicyEvaluation` records. Tools return the evaluation; they do
   not accept one as input.
4. **LIMS writes require a stored policy-evaluation id.** `DemoLims.write_disposition` re-reads the
   evaluation from the repository (`lookup_evaluation`) and refuses when it is absent, differs from
   the object presented, is not `ALLOWED`, or belongs to another sample. Identity overwrite is
   refused unconditionally; a barcode collision can only lead to quarantine.
5. **Every state change goes through `TransitionService`.** Domain models are frozen; the service
   validates the transition table, requires a policy-evaluation id for any terminal disposition,
   bumps the case version and writes an audit event with actor, reason code and evidence refs.
6. **Untrusted text is data.** Uploaded documents are parsed into typed fields; free text is stored
   under `metadata.untrusted_text` and never interpreted as instructions. A sender's correction is a
   *proposal* until `EvidenceService._admit_correction` verifies token, contact, request, row,
   near-match status and collision-freedom.

## Consequences

- The demo can show the agent doing genuinely agentic work (reading notes, consolidating,
  choosing a verified recipient, re-running only affected checks) while the video can say, truthfully,
  "it cannot manufacture evidence or cross a scientific boundary."
- Some flexibility is lost: the agent cannot "use judgment" to accept a borderline sample. That
  is the point.
