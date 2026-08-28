"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type VerificationReport } from "@/lib/api";
import { Card, ErrorBox, Skeleton } from "@/components/ui";
import { DiffedValue, glyphDifference } from "@/components/identifiers";

/* ------------------------------------------------------------------------------------------------
   The Shipment Verification Report.

   ISBER §J6 and §L4.5 specify this artifact and its contents; NCI §C.2.10 makes sending it an
   obligation, discrepancies, damage and condition deviations "should be documented and reported
   immediately to the sender". In practice it is filled in by hand hours later, if at all.

   It reads as a document rather than a dashboard on purpose. The audience is a site coordinator who
   wants to know what arrived and what happened to it, and an auditor checking the lab against the
   standard, neither of whom wants a chart. Where the paper form has a signature and a handwritten
   resolution, this carries the attributed actor, the reason, and the policy version that decided.
------------------------------------------------------------------------------------------------ */

function Line({ label, value, tone }: { label: React.ReactNode; value: React.ReactNode; tone?: "warn" | "fail" | "pass" }) {
  const cls = tone === "warn" ? "text-warn-fg" : tone === "fail" ? "text-fail-fg" : tone === "pass" ? "text-pass-fg" : "text-fg";
  return (
    <div className="flex flex-wrap justify-between gap-x-6 gap-y-0.5 border-b border-border/60 py-1.5 last:border-0">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className={`text-sm ${cls}`}>{value}</dd>
    </div>
  );
}

function Section({ title, clause, children }: { title: string; clause?: string; children: React.ReactNode }) {
  return (
    <section className="mt-5 first:mt-0">
      <h3 className="text-sm font-semibold uppercase tracking-wider text-fg-muted">{title}</h3>
      {clause && <p className="mt-0.5 text-[13px] text-fg-subtle">{clause}</p>}
      <dl className="mt-2">{children}</dl>
    </section>
  );
}

function ids(list: string[]): React.ReactNode {
  if (!list.length) return <span className="text-fg-subtle">none</span>;
  return <span className="font-mono text-sm">{list.join(", ")}</span>;
}

export function VerificationReportView({ caseId }: { caseId: string }) {
  const [report, setReport] = useState<VerificationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setReport(await api.verificationReport(caseId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [caseId]);

  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  if (error) return <ErrorBox error={error} onRetry={load} />;
  if (!report) return <Skeleton className="h-64 w-full" />;

  const { receipt, condition, reconciliation: rec, disposition: disp } = report;

  return (
    <Card
      title="Shipment verification report"
      right={
        <span className="text-sm text-fg-subtle">
          {report.complete ? "final" : "provisional, intake is still open"}
        </span>
      }
    >
      <p className="text-sm text-fg-muted">
        What the receiving lab owes {receipt.sending_site ?? "the sending site"} for {report.shipment_id}.
        Every line is read from the records written while the work was done, so it cannot say something
        different from what happened.
      </p>

      <Section title="Receipt" clause={report.clauses.receipt}>
        <Line label="Sending site" value={receipt.sending_site ?? "-"} />
        <Line label="Courier" value={receipt.courier || "-"} />
        <Line label="Tracking reference" value={<span className="font-mono text-sm">{receipt.tracking_reference || "-"}</span>} />
        <Line label="Received" value={receipt.received_at ? new Date(receipt.received_at).toLocaleString() : "not yet received"} />
        <Line label="Recorded by" value={receipt.received_by ?? "-"} />
      </Section>

      <Section title="Condition on arrival" clause={report.clauses.condition}>
        <Line
          label="Package condition"
          value={(condition.package_condition ?? "-").replaceAll("_", " ").toLowerCase()}
          tone={condition.package_condition === "ACCEPTABLE" ? "pass" : "fail"}
        />
        <Line label="Tamper seal" value={condition.seal_intact ? "intact" : "not intact"} tone={condition.seal_intact ? "pass" : "fail"} />
        <Line label="Refrigerant on opening" value={condition.refrigerant_condition || "not recorded"} />
        {condition.temperature_at_reception_c !== null && (
          <Line label="Temperature at reception" value={`${condition.temperature_at_reception_c} °C`} />
        )}
        <Line
          label="Containers"
          value={`${condition.containers_received ?? "-"} received of ${condition.containers_declared ?? "-"} declared`}
          tone={condition.container_count_matched ? "pass" : "fail"}
        />
        <Line label="Temperature logger files" value={condition.logger_files_received} tone={condition.logger_files_received ? "pass" : "warn"} />
        {condition.specimen_condition_notes.length > 0 && (
          <Line
            label="Specimens not in acceptable condition"
            tone="warn"
            value={
              <span className="font-mono text-sm">
                {condition.specimen_condition_notes.map((n) => `${n.sample_id} (${n.received_quality.replaceAll("_", " ").toLowerCase()})`).join(", ")}
              </span>
            }
          />
        )}
        {condition.condition_notes && <Line label="Notes" value={condition.condition_notes} />}
      </Section>

      <Section title="Reconciliation against the manifest" clause={report.clauses.reconciliation}>
        <Line
          label="Specimens"
          value={`${rec.received} received of ${rec.declared} declared`}
          tone={rec.received === rec.declared ? "pass" : "fail"}
        />
        <Line label="Declared but not received" value={ids(rec.not_received)} tone={rec.not_received.length ? "fail" : undefined} />
        <Line label="Received but not on the manifest" value={ids(rec.not_on_manifest)} tone={rec.not_on_manifest.length ? "fail" : undefined} />
        <Line label="Duplicate identifiers" value={ids(rec.duplicate_identifiers)} tone={rec.duplicate_identifiers.length ? "fail" : undefined} />
        {rec.identifier_near_matches.map((n) => (
          <Line
            key={n.row}
            label={`Row ${n.row} identifier`}
            tone="warn"
            value={
              <span>
                <DiffedValue declared={n.read_on_tube} scanned={n.declared} /> on the manifest,{" "}
                <DiffedValue declared={n.declared} scanned={n.read_on_tube} /> on the tube
                <span className="mt-0.5 block text-[13px] text-fg-muted">
                  {glyphDifference(n.declared, n.read_on_tube)}
                </span>
              </span>
            }
          />
        ))}
      </Section>

      <Section title="Discrepancies and resolutions" clause={report.clauses.discrepancies}>
        {report.resolutions.length === 0 ? (
          <p className="py-1.5 text-sm text-fg-subtle">Nothing needed resolving.</p>
        ) : (
          report.resolutions.map((r, i) => (
            <Line
              key={i}
              label={<span className="font-mono text-sm">{r.sample_id}</span>}
              value={
                <span>
                  <span className="font-medium">{r.resolution.replaceAll("_", " ").toLowerCase()}</span>
                  <span className="text-fg-muted">, {r.settled_by}</span>
                  {r.comment && <span className="mt-0.5 block text-[13px] text-fg-muted">{r.comment}</span>}
                </span>
              }
            />
          ))
        )}
      </Section>

      <Section title="Disposition" clause={report.clauses.disposition}>
        <Line label="Accepted" value={ids(disp.accepted)} tone={disp.accepted.length ? "pass" : undefined} />
        {disp.accepted_with_exception.length > 0 && (
          <Line label="Accepted with a documented exception" value={ids(disp.accepted_with_exception)} tone="warn" />
        )}
        <Line label="Held" value={ids(disp.held)} tone={disp.held.length ? "warn" : undefined} />
        {disp.rejected.length > 0 && <Line label="Rejected" value={ids(disp.rejected)} tone="fail" />}
        {disp.still_open.length > 0 && <Line label="Still open" value={ids(disp.still_open)} tone="warn" />}
        <Line
          label="Decided under"
          value={<span className="font-mono text-sm">{disp.policy}</span>}
        />
      </Section>

      <p className="mt-5 border-t border-border pt-3 text-[13px] leading-relaxed text-fg-subtle">
        Where a paper form carries a signature and a handwritten resolution, this carries the named actor,
        the reason, and the version of the acceptance criteria that decided. Acceptance is authorised by
        that versioned policy, never by the language model, ISO 20387 §7.3.2.2 requires acceptance criteria
        to be defined and verified on reception, and this is that requirement made executable.
      </p>
    </Card>
  );
}
