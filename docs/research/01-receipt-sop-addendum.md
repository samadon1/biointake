# Research: receipt SOPs, normative clauses and throughput

Run 2026-08-27. Clause numbers verified against NATA's ISO 20387 assessment worksheet, ISO front-matter
previews and a licensed full copy; flags preserved from the researcher.

## ISO 20387:2018 §7.3.2 "Reception", the clause that names accessioning

> **7.3.2.1** "The biobank shall establish, document and implement procedures for receiving or acquiring
> biological material and associated data… **NOTE Such procedures are sometimes referred to as
> accession/logging procedures.**"

| Clause | Requirement | Consequence for us |
|---|---|---|
| **7.3.2.2** | identification **shall be verified upon reception** against *defined acceptance criteria* (covering biosafety, biosecurity, IP rights) | acceptance criteria are a configurable per-collection ruleset evaluated at intake, which is exactly our policy engine |
| **7.3.2.4** | received material *"shall be segregated… **to prevent final storage** until legal, ethical, documentation, and quality compliance has been assessed and managed"* | **quarantine is normative, not advice**, every sample is held until cleared, not only problem ones |
| **7.4.2** | chain of custody dispatch→receipt, detailing deviations per 7.11 |, |
| **7.5.1(a)–(f)** | persistent unique-ID tagging · permissions/restrictions per item · inventory that lets deviations be **flagged** · maintained material↔data link · **location identifiable at all times** · ability to identify already-distributed/disposed material | location is a first-class field; "flagged" corroborates status-plus-annotation |
| **7.11.1.5** | nonconforming output: responsibilities · significance **including effect on further use** · acceptability / segregation / containment / return / suspension / recall · persistent nonconformity · communication and **recipient authorisation for acceptance** | "accept with deviation, recipient informed" is an **explicitly sanctioned outcome** |
| **Annex A (normative)** | A.2 Acquisition, A.3 Transport: mode of transport, temperature **during** transport, **temperature or range at reception**, transport start/end date-time in **ISO 8601** | our minimum intake field set, normatively |

## ISO/TS 20658:2017 §17, the clinical side, and a genuine split

Two citation corrections: the 2017 document is a Technical **Specification**; and **ISO 20658:2023 deleted the
sample-receipt clause entirely** (its 7.4 concerns receiving the *patient*). For a current-edition citation on
sample acceptance/rejection use **ISO 15189:2022 §7.2**.

- **17.2, twelve enumerated rejection grounds**: improper handling/transport · unlabelled or mislabelled ·
  label/form discrepancy · missing unique identifiers · wrong anticoagulant, wrong blood-to-additive ratio,
  wrong medium or unsuitable type · mixed/contaminated · missing information to judge appropriateness ·
  exposure to extreme temperatures · insufficient volume · unsuitable container · damaged container and/or
  haemolysis · collection-to-arrival time exceeding the stated interval. Notify the authorised person without
  delay; *"the reason for refusing to accept each sample shall be documented."*
- **17.3.2, samples received unlabelled or mislabelled shall NOT be relabelled by laboratory personnel after
  arrival.** Authorised changes must record both the person making and the person authorising it.
  **This contradicts CAP BAP.03100**, which permits relabelling in a research biobank with a documented reason.
  → **policy toggle, not hardcoded behaviour**: diagnostic receipt forbids it, research biobanking allows it.
- **17.4, ten mandatory receipt-record fields**: patient identity · sample id / **accession number** ·
  date, time and identity of collector · **date and time received by the laboratory** · **identity of the
  receiver** · sample type · container type for fluids · for solid tissue: **warm ischaemia time,
  extracorporeal storage time, fixation type and time** · quality comments (haemolysis, insufficient quantity,
  drawn above an IV line) · **details of rejected samples and the reason**.
- **17.5** numbering must make two same-numbered samples impossible in the lab simultaneously.
- **17.7** an accepted sample's location determinable at any time, with a controlled record of everyone who
  handled or transferred it, dates and times, retained as potential evidence.

⚠️ Clause numbers verified in English; body text translated from the Russian identical adoption
(GOST R 59787-2021) because the English body is paywalled. Verify wording before quoting verbatim.

## Throughput; the gap is real

No published time-motion study of biospecimen receipt exists. The clearest evidence: a multicentre laboratory
time-and-motion protocol **explicitly excludes** it, its "tasks not to be recorded" list reads *"Opening of post
(receipt of samples)"*. Treat any per-receipt minute figure as vendor marketing.

Verified adjacent numbers: a published receipt SOP (CONSTANCES/IBBL) is **five steps**, inspect tube integrity
(readable barcode, undamaged, sufficient volume) → **scan tube in the Reception application (receipt timestamp)** →
**check count received vs expected** → **verify transport conditions (packing, temperature)** → sort by type.
Courier window 30 h from collection; collection→cryopreservation guarantee 36 h; automation sized for
100–120 participants/day at 26 aliquots each; max processing after receipt: urine 1 h 30, serum & heparin plasma
2 h, EDTA plasma & buffy coat 3 h. UK Biobank archive ≈15 M aliquots. Logging one deviation takes 15–60 seconds.

## Error rates at receipt, for sizing the exception path

- **6% overall rejection at reception** (27,067 of 453,171 samples, 12 months). By group: coagulation 13.3% ·
  TDM 12.8% · hormones 12% · blood gases 9.8% · urinalysis 9.2% · cardiac markers 3.5% · CBC 3.2% ·
  biochemistry 2.5%. Top causes: fibrin clots 28%, inadequate volume 9%.
- **Patient-ID error rate 0.0511% → 0.0015%** over ten years after a restrictive acceptance policy plus barcode
  positive ID, a 97% relative reduction. Published cross-institution range 0.005%–1.12%.
- A biorepository logged 569 errors in 12 months (84 critical / 50 major / 433 minor) from 7–10 staff;
  **56 of the 84 critical were privacy leaks in received pathology documents** (surgical pathology numbers,
  clinician names). No specimen denominator, so no per-1000 rate is derivable.

## Still unverifiable
Time per receipt (absent from the literature, not merely unfound) · specimens per shipment · any published
"accessioned within N hours" target · the KIMMS rate denominator · English body text of ISO/TS 20658 §§6–22 ·
CLSI GP44's "2 hours".
