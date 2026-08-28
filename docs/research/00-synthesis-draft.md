# Workflow synthesis, working draft

Status: **draft, 2 of 4 research streams in.** Kept as a working note; the redesign proposal supersedes it.

## What we now know we got wrong

| # | Our model | What practice does | Source |
|---|---|---|---|
| 1 | `ACCEPTED_WITH_EXCEPTION` | phrase occurs **zero times** in ISBER / NCI / CAP BAP / ISO 20387. Attested: **condition exception**, **condition warning** (CAP BAP.03000, BAP.12800 item 15), **concession** (ISO 9000 3.12.5) | cold-chain |
| 2 | `QUARANTINED` is terminal | ISBER quarantine is **transient**, "quarantine storage … while any discrepancies or missing information are being resolved". Terminal refusal is *rejection* | cold-chain |
| 3 | seven checks evaluated as a flat set | the **logger is read before the manifest is reconciled**, and the sample is moved to quarantine storage immediately | cold-chain |
| 4 | one temperature number | three independent gates: **peak**, **cumulative minutes out**, **longest continuous run** (LogTag instant / accumulative / consecutive) | cold-chain ✅ *applied* |
| 5 | status and condition fused in one enum | CAP models **status** (BAP.12800: reserved/available) **separately from condition warnings** | cold-chain |
| 6 | disposition ends at the LIMS write | ISBER J6 + IARC SOP 01: a **Shipment Verification Report goes back to the shipper**; notification is mandatory | cold-chain |
| 7 | optional free-text comment | **impact assessment** is required (CAP BAP.09300) on three axes: specimen integrity / study data / subject safety | cold-chain |
| 8 | evidence request has one state | EDC query model: **Draft → Sent → Responded → Resolved / Cancelled**, `Reopened`; an unanswered request may be **cancelled but never closed** | discrepancy |
| 9 | requirements satisfied silently | resolution needs a **closed vocabulary** code + a first-class attachment slot | discrepancy |
| 10 | no age anywhere | **age is the primary sort key**: median ~23 days, mean ~52, 21% need a second round | discrepancy ✅ *applied* |
| 11 | PI approves the exception | authority **depends on the study**: sponsor decides in sponsored trials (CAP BAP.01700); director/designee in an academic biobank. Ours is the academic branch and should say so | cold-chain |
| 12 | escalation implied | first notification goes to the **site study coordinator**, not the PI; escalation is triggered by **recurrence or severity** | cold-chain |

## Applied so far
4 (three excursion numbers) · 10 (age clock + buckets) · 1 partially (UI labels now use attested vocabulary;
the enum still says `ACCEPTED_WITH_EXCEPTION`).

## Still to decide (needs the remaining two streams)
- Whether to split `QUARANTINED` into a transient **hold** and a terminal **rejected**, touches the state
  machine, the disposition engine and the fixture's expected counts.
- Whether to model a **deviation record** as a first-class entity (it has ~30 attested fields) or keep the
  audit trail as the record of exception.
- Whether the evidence request becomes a **bundle of individually-resolvable items** with draft/cancel states.
- Storage location, aliquots and accessioning, waiting on the receipt-SOP and LIMS-UX streams.
