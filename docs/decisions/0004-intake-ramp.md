# ADR 0004, The intake ramp: how a shipment actually enters the system

**Status:** accepted (Phase 4) · **Date:** 2026-08-27

## Context

The agent, the deterministic acceptance engine and the resumable human interrupt all work. But the only way a
shipment entered the system was a fixture loader that materialised a manifest, a scanner export, two logger
files, a consent registry, a custody log and pre-registered LIMS records, none of which exist when a box
actually lands on the dock. The product had no front door.

Research established the real sequence (`docs/research/01`, `02`):

0. **Advance notification is mandatory**, not a courtesy, the shipper notifies, the recipient confirms
   capacity and staffing before the courier is booked (ISBER L4.2; CAP BAP.13200).
1. Physical receipt: custody signed, seals checked, condition and refrigerant recorded, damage photographed.
2. **The logger is read inside the receiving inspection pass**, before the manifest is reconciled.
3. Reconciliation against the manifest, then identifiers, then storage location.

And two interaction patterns worth copying exactly:
- **The manifest defines the rows; the scanner fills one column** (Nautilus), discrepancy detection becomes free.
- **Nothing is written to inventory directly**; scans land in a staging batch that must be explicitly committed,
  with warnings and errors shown inline (BSI).

## Decision

Add the four steps that precede the agent, as first-class states and screens:

```
ANNOUNCED ──► RECEIVED ──► VERIFYING ──► WAITING_FOR_EVIDENCE / NEEDS_HUMAN_DECISION ──► COMPLETED
   │             │             ▲
   │             │             └── committed staging batch creates the samples
   │             └── receipt record: condition, package count, refrigerant, logger files
   └── pre-notification: manifest + courier + logger ids, validated against the study before shipping
```

**It must work with zero integrations on day one.** A small biobank has email, a spreadsheet, a handheld scanner
and perhaps Freezerworks; it does not have an integration budget. So every input is a file, a form, or a scan:

| Input | Day one | Later |
|---|---|---|
| Manifest | CSV upload by the sending site | EDC / LIMS export |
| Tube identities | **keyboard-wedge scanner** in the browser, or a rack-reader CSV | 2D reader drivers (no vendor documents these; the real architecture is decoder-writes-CSV) |
| Temperature | logger CSV upload | cloud logger API |
| Consent / protocol | registry maintained by the study owner, plus uploaded documents | REDCap / EDC |
| LIMS | demo adapter behind a documented interface | site's own LIMS |

**Site access is by magic link, not accounts.** Onboarding friction at the sending site is the single most
likely reason a request goes unanswered; the research shows the coordinator is already juggling several trials.
The link is scoped to one shipment or one evidence request and carries its own token.

**Study configuration becomes data.** `default_policy()` moves into a `Study` record a lab can edit: temperature
rule, consent requirement, custody events, exception-approval role, site contacts, and the segment toggles from
ADR 0003.

## Consequences

- `CaseState` gains `ANNOUNCED` and `RECEIVED` before `VERIFYING`; `PARTIALLY_RECEIVED` is expressed by the
  receipt record (scanned < expected), which the research shows is a real, separately-modelled state.
- The demo stops bypassing the ramp: it plays *through* announcement, receipt and scanning, so what a judge
  sees is what a lab would do.
- The fixture becomes the *sending site's* data (a manifest a site would upload), not the receiving lab's
  pre-loaded truth.
