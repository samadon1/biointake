# Research: how labs actually chase missing paperwork

Source study run 2026-08-27. Confidence flags are the researcher's own; primary sources are cited inline.

## Channels, ranked
1. **Email to a shared/study mailbox**, dominant, because the request crosses an organisational boundary and must carry an attachment (the scanned consent, the corrected manifest).
2. **Phone**, not legacy colour: CAP Biorepository Accreditation **BAP.14200** requires remote-site contact information be available "at all times to resolve discrepancies". A multi-site informatics paper states discrepancy resolution "generally involves human intervention (e.g. phone calls to collection centers)".
3. **EDC query** (Medidata Rave, Oracle InForm, REDCap DRW), the only channel with a real audit trail, but only reaches items modelled as eCRF fields.
4. **Vendor portal** (LabConnect SampleGISTICS, Slope Biospecimen360), real but per-sponsor; sites juggle several.
5. **Ticketing (Jira/Zendesk)**, no evidence found. Treat as false.

## The EDC query model (candidate model for our evidence requests)
States, converged across three systems:
`Candidate/Draft` → `Open/Sent` → `Answered/Responded` → `Closed/Resolved` | `Cancelled`, plus `Reopened`.
Two rules worth stealing:
- **Draft before send**, a human reviews auto-detected discrepancies before anything leaves the building (~72% of EDC queries produce no data change; restraint is documented best practice).
- **An unanswered query can be cancelled but never closed**, "we gave up" cannot masquerade as "resolved".

Fields: anchor (subject+event+field), status + full status history, opened by/at, assigned to, query text,
**closed-vocabulary response type**, response comment, **attached file**, days open, interleaved data changes.
REDCap's response vocabulary: *Corrected – data missing · Corrected – typographical error · Corrected – wrong source used · Verified – confirmed correct · Other*.
Role asymmetry: the site can **respond** but usually cannot **close**.

## Who receives it, and the hidden hop
The study coordinator (CRC), overwhelmingly, while juggling several trials and patient care. Structural problem
stated at source: *"the person filling out a requisition form may not be the same person who collected the samples"*.
Every request therefore has an unlogged internal forwarding hop at the site.

## Turnaround: targets vs reality
| | |
|---|---|
| SOP targets | first response 3–5 business days; resolution 5–10; critical 1–2; ≥90% on-time |
| Observed | **median 23 days, mean ~52**, tail to 22.8 weeks |
| Rework | **21%** of queries needed resubmission, the first answer did not resolve it |
| Effort | ~10 min per simple query; 5,000 queries ≈ 833 staff-hours |
(Actuals via a secondary aggregator citing Tolmie 2011 / Pronker 2011, directionally trustworthy, do not quote externally without the primaries.)

## Batched vs individual
**Create per problem, deliver per person.** Queries anchor to one data point; sites work from a batched dashboard
(REDCap "Resolve Issues", Rave Task Summary). A reply must be able to **close 3 of 9 items and leave 6 open**,
EDC offers no prior art for that bundle layer.

## Write-back requirements (CAP / NCI)
- **BAP.13300**, discrepancies from manifest, specimen damage and confirmation of receipt are *fields of the shipping record*.
- **BAP.13500**, discrepancies are documented and reconciled **prior to distribution** (i.e. a release block).
- **BAP.03000**, documented mechanism to notify, note the condition on the report, record the dialogue held.
- **NCI §C.2.1.1**, track whether consent is present, or why it is not needed; resolve consent-status discrepancies.
- **NCI §B.2.8.2.2**, on receipt, verify labels and accompanying documents against the packing list. *(This is the moment our requests are generated.)*
- CAP inspector question, verbatim: *"What action is taken if a sample is received without the proper informed consent documentation?"*

## What goes wrong
Ping-pong/partial answers (21% resubmission) · the wrong person holds the answer · transcription errors at requisition ·
latency destroys context (metadata reconciled months later) · **blocking**: unresolved queries halt processing, shipment
and reporting · noise fatigue (~72% no-change) · the >90-day tail loses its documented cause.

## Design implications
1. **Two levels: Request (per item) inside Bundle (per recipient per send)**, with partial resolution.
2. **Draft state + cancel-with-reason**; never close an unanswered request.
3. **Closed-vocabulary resolution codes with a first-class attachment slot.**
4. **Age is the primary sort key**, visible in the list: buckets 0–5 / 6–14 / 15–30 / 30+ / 90+, with an escalation action (documented path is upward to PI and CRO).
5. **Every request writes back to the specimen record**, which carries a paperwork-completeness state and a **release block** while a request is open. Build the audit view around the *specimen*, because that is what an inspector selects.
