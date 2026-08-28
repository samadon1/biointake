# ADR 0003, The first real user is an academic biobank

**Status:** accepted (Phase 4) · **Date:** 2026-08-27

## Context

Until now BioIntake had no defined customer: the domain was built from the shape of the problem, and the
demo entered through a fixture. Four research streams (see `docs/research/`) showed that several rules fork
by segment, so "a lab" is not specific enough to design against.

The forks that matter:

| Question | Academic biobank | Diagnostic / CLIA lab | Sponsored trial / CRO |
|---|---|---|---|
| Relabelling an incoming tube | permitted **with documented reason** (CAP BAP.03100) | **forbidden** after arrival (ISO/TS 20658 §17.3.2) | site/sponsor SOP |
| Who decides usability after an excursion | **director / designee**, in dialogue with the requesting researcher | laboratory director | **the sponsor**, the lab executes instructions and has no local discretion (CAP BAP.01700) |
| Governing standards | ISBER BP, NCI Best Practices, CAP BAP, ISO 20387 | CLIA 42 CFR 493, CAP LAP, ISO 15189 | GCP/GCLP + sponsor manual |

## Decision

**The first user is an academic biobank / research biorepository receiving shipments from multiple collection
sites**, on the ISBER / NCI / CAP BAP / ISO 20387 standard set.

Reasons:
1. **The human judgement moment is real there.** In a sponsored trial the sponsor decides and the receiving lab
   merely carries out instructions; our entire design, an agent that escalates a decision to a person with
   authority, only makes sense where that person actually holds the authority.
2. **It is the segment with the least software budget and the most manual reconciliation.** Commercial LIMS
   receiving is already adequate (SUS 59.7, and *receiving* is among its best-rated tasks); what is missing
   everywhere is the reconciliation and the chasing, which is precisely what a small biobank does by hand.
3. **We are not validated for diagnostics.** CLIA imposes obligations we have not built and should not imply.

## Consequences

- Segment-specific rules are **policy toggles on the study**, not hardcoded behaviour: `relabelling_permitted`
  (default true, with a mandatory reason), and the exception-approval role (default principal investigator).
- The README states the segment plainly, and states that a sponsored-trial deployment would need the sponsor
  as the disposition authority.
- Vocabulary follows biobanking, not pathology: the field says **receipt / receiving / inventory acceptance**;
  "accessioning" appears zero times in NCI Best Practices 4th ed. (ISO 20387 §7.3.2.1 notes it only as an alias).
- **No user research has been done.** Everything here is inferred from standards and published SOPs, which
  describe what labs are audited on rather than what they do at 4pm on a Friday. Every inferred decision is
  marked `# INFERRED:` in the code so it is cheap to correct after the first real conversation.
