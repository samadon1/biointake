# PROTO-042 Research Plasma Intake Policy

Policy `POLICY-PROTO-042` version `3.0.0` for protocol `PROTO-042`.

- Allowed specimen types: PLASMA
- Required checks: IDENTITY_MATCH, MANIFEST_MATCH, PROTOCOL_ELIGIBILITY, CONSENT_VALIDITY, TEMPERATURE_REQUIREMENT, CHAIN_OF_CUSTODY, LIMS_RECONCILIATION
- Transport temperature: 2.0–8.0 °C, cumulative tolerance 10.0 min; §7.3, a cumulative excursion above tolerance requires a documented principal-investigator disposition.
- Consent: version ≥ 3, scope `RESEARCH_PLASMA`; §4.1, consent addendum v3 or later is required for plasma research use.
- Custody events (ordered): COLLECTED → PACKED → SHIPPED → RECEIVED; §5.2, every handoff must be recorded with actor and timestamp, in order.
- Quarantine may be directed by: COORDINATOR, PRINCIPAL_INVESTIGATOR, QA_REVIEWER
- Temperature exception may be approved by: PRINCIPAL_INVESTIGATOR

All data in this demonstration is synthetic.
