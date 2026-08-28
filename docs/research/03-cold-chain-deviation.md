# Research: temperature excursions and the deviation process

Run 2026-08-27 against primary standards text (ISBER BP 5th ed., NCI Best Practices 2016 incl. the CAP
BAP checklist as Appendix 6, ISO 20387:2018, ICH E6(R3), WHO GCLP). Load-bearing quotes verified in source.

## Three findings that change our design

1. **"Accepted with exception" is a real practice but not a real phrase.** Zero occurrences of
   "accepted with exception", "conditionally acceptable" or "restricted use" across ISBER (196pp), NCI (150pp),
   the CAP BAP checklist and ISO 20387. The attested vocabulary is CAP's **condition exception / condition
   warning** and ISO 9000's **concession** ("permission to use or release a product … that does not conform").
2. **ISBER's quarantine is transient, not terminal.** *"Specimens should be kept in an environmentally-controlled
   temporary storage location (also termed quarantine storage) … while any discrepancies or missing information
   are being resolved."* Quarantine is where a sample sits **pending** resolution; the terminal refusal is rejection.
3. **Mean Kinetic Temperature does not belong here.** `kinetic` and `MKT` appear zero times in all four
   biobanking corpora; MKT is a finished-drug-product construct and is invalid across a glass transition.

## The excursion workflow (verified order)

| # | Step | Role | Timing |
|---|---|---|---|
| 1 | Inspect package; photograph container and refrigerant | Receiving tech | On arrival |
| 2 | **Read the logger before reconciling the manifest** | Receiving tech | Before shelving |
| 3 | **Move to quarantine storage while discrepancies are unresolved** | Receiving tech | Immediately |
| 4 | Write the event to the specimen records | Receiving tech | Same session |
| 5 | Complete a Shipment Verification Report including logger output | Receiving tech | Same day |
| 6 | **Notify the shipper**, returning a copy of that report | Repository → site coordinator | 24h (institutional convention) |
| 7 | Impact assessment against pre-defined criteria | Director/designee or sponsor | SOP-defined |
| 8 | Adjudicate disposition | see below | SOP-defined |
| 9 | Annotate the specimen permanently; disclose to the end user | Repository | At disposition |
| 10 | If systemic: root cause + CAPA; escalate | QA |, |

**Who is told first is counter-intuitive.** ACTG/IMPAACT §9.4: *"The study coordinator at the shipping site should
be notified of any problems… It is not necessary to contact the Principal Investigator unless the problems are
ongoing or if there was a major rule violation."* Escalation is triggered by recurrence or severity, not by the
excursion itself. **No regulator sets a clock** for specimen excursions, ICH E6(R3) and WHO GCLP say "promptly";
every hard number in circulation is institutional convention.

## Who decides; it depends on the study

CAP **BAP.01700**, verbatim: *"If samples are acquired according to sponsor-driven protocols, the sponsor makes all
decisions about sample usability. The biorepository carries out the instructions provided by the sponsor."*

| Setting | Quarantine | Final disposition |
|---|---|---|
| Sponsored trial | receiving lab, unilaterally | **sponsor**; the lab has no local discretion |
| GCLP facility | lab | APM + sponsor jointly, dated signatures |
| Academic biobank | repository staff | **director/designee**, in dialogue with the requesting researcher |
| NIH network | site lab | lab director implements; PI responsible; DAIDS adjudicates critical |

Our PROTO-042 policy (PI approves the exception) is the **academic-biobank branch** and should say so.

## The deviation record

Identity · three distinct dates (occurrence / detection / report) · discovered_by, recorded_by, **responsible authority**
(ISO 20387 7.11.1.5a) · affected specimens, subject, site, study, shipment, manifest ref · description, category,
**severity** · **impact assessment on three axes, specimen integrity, study data, subject safety** (CAP BAP.09300
names it explicitly) · effect on further use · third-party results affected · containment (segregation / return /
suspension / recall) · disposition + condition warning + recipient notified/authorised · decision maker, role,
justification · **root cause as a list** (ISO 9000: "there can be more than one cause") · **similar nonconformities
exist?** (ISO 8.7.1c, commonly missed) · corrective vs preventive action · effectiveness check · closure/signatures.

## What counts as an excursion

Three independent constructs, not interchangeable: **single-point/peak** (the regulatory default), **cumulative time
out of range**, with the sub-distinction that matters most, **consecutive** (contiguous) vs **accumulative** (summed),
and **MKT** (drug products only). *"Not to exceed 4 hours above 8 °C" is underspecified until you say which.*
Acceptability is set by manufacturer stability data; for biospecimens there usually is none, the specimen *is* the
experiment, so practice records **peak, cumulative time and thaw count**, annotates and escalates.

## CAP's data model: status + annotation, never fused

BAP.12800 tracks *biospecimen status* (reserved/available) **separately from** *condition warnings*. And BAP.03000:

> "This requirement is **not intended to imply that all 'unacceptable' specimens be discarded or not analyzed**.
> For example, if an unacceptable specimen is received, there must be a mechanism to notify the requesting
> researcher, and to note the condition of the sample on the report."

## Deltas for BioIntake

| # | Finding | Change |
|---|---|---|
| a | `ACCEPTED_WITH_EXCEPTION` is invented vocabulary | keep the state, relabel: **"Accept with documented condition exception"**, and say the warning travels with the sample to every downstream user |
| b | Quarantine is transient in ISBER; ours is terminal | model a pre-decision **hold** (from the moment the logger is read) distinct from terminal rejection |
| c | No impact assessment on the card | make justification **mandatory and structured** on the three attested axes for an accept-with-exception |
| d | `temperature.py` computes `longest_continuous_minutes` but never gates on it | surface **three numbers**, peak °C, cumulative minutes, longest continuous run, and gate on the ones the policy names |
| e | No "notify the sending site" step | disposition should emit a **Shipment Verification Report** back to the sender (ISBER J6 makes this mandatory) |
| f | No sponsor role | note in the policy that PI-approval is the academic-biobank branch; CAP BAP.01700 gives sponsors that authority in sponsored trials |
| g |, | **do not add MKT** |

## Unverified (flagged by the researcher)
CLIA lab-director authority (inferred) · "within 1 business day" / "5 days for investigation" (no public source) ·
CAP item numbers are from the 2013/14 revisions reproduced in NCI 2016 Appendix 6, current editions are behind
e-LAB Solutions · CLSI QMS11 and ISO 15189 field lists are paywalled · commercial central-lab manuals are not public.

## Key sources
ISBER Best Practices 5th ed · NCI Best Practices for Biospecimen Resources 2016 (CAP BAP checklist, Appendix 6) ·
ISO 20387:2018 · ICH E6(R3) · WHO GCLP · WHO TRS 961 Annex 9 · USP <1079.2> · ACTN/DAIDS cold-chain guidelines ·
IARC Biobank SOP 01 · PPMI Biologics Manual Appendix J · NIDCR protocol-deviation log · LogTag alarm configuration.
