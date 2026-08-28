"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  pollInterval,
  CHECKS,
  CHECK_LABELS,
  type AuditEvent,
  type CaseView,
  type CheckDetail,
  type OutboxMessage,
  type PendingDecision,
  type RunResult,
  type TemperatureSeries,
} from "@/lib/api";
import { AgentPulse, Button, Card, CheckCell, DISCREPANCY_SLA_HOURS, Elapsed, EmptyState, ErrorBox, PageError, RunResultCard, Skeleton, StateBadge, ageOf } from "@/components/ui";
import { CheckProgress, DispositionBar, TemperatureTrace } from "@/components/charts";
import { PageHeader } from "@/components/shell";
import { VerificationReportView } from "@/components/verification-report";

const DOMAIN_LABELS: Record<string, string> = {
  CASE_CREATED: "Case created",
  CASE_TRANSITION: "Case state",
  SAMPLE_TRANSITION: "Sample",
  CHECK_RECORDED: "Checks evaluated",
  POLICY_EVALUATED: "Policy engine",
  LIMS_WRITE: "LIMS updated",
  EVIDENCE_REQUEST_SENT: "Evidence requested",
  EVIDENCE_RECEIVED: "Evidence admitted",
  EVIDENCE_REJECTED: "Evidence rejected",
  EVIDENCE_REQUEST_SATISFIED: "Request satisfied",
  INVALIDATION_PLAN_CREATED: "Re-verification plan",
  INVALIDATION_PLAN_APPLIED: "Re-verified",
  PENDING_DECISION_CREATED: "Human decision requested",
  HUMAN_DECISION_RECORDED: "Human decision",
  HUMAN_DECISION_APPLIED: "Decision applied",
  CASE_FINALIZED: "Case completed",
  INTERVENTION_DENIED: "Blocked by policy",
  TOOL_ATTEMPT: "Tool call",
  TOOL_RESULT: "Tool result",
  MODEL_CALL: "Model call",
  INVOCATION_STARTED: "Invocation started",
  INVOCATION_FINISHED: "Invocation finished",
  OPERATION_REPLAYED: "Duplicate ignored",
  OPERATION_REJECTED: "Rejected",
  RETRY_REQUESTED: "Retry",
  RETRY_REFUSED: "Retry refused",
  // The receiving bench, which is the first thing on a case timeline and was showing raw enum
  // names because nobody had named them here.
  SHIPMENT_ANNOUNCED: "Shipment announced",
  SHIPMENT_RECEIVED: "Shipment received",
  SCAN_RECORDED: "Tube scanned",
  SPECIMEN_QUALITY_RECORDED: "Condition recorded",
  STAGING_BATCH_COMMITTED: "Batch committed",
  QUARANTINE_REVIEW_OPENED: "Quarantine reopened",
  LIMS_WRITE_REFUSED: "LIMS write refused",
  STUDY_SAVED: "Study saved",
  CONTACT_REGISTERED: "Site contact added",
};

/** Phases group the timeline so a reader can follow the story rather than a flat log. */
const PHASE_OF: Record<string, string> = {
  CASE_CREATED: "Intake",
  CHECK_RECORDED: "Verify",
  POLICY_EVALUATED: "Decide",
  LIMS_WRITE: "Decide",
  SAMPLE_TRANSITION: "Decide",
  CASE_TRANSITION: "Decide",
  EVIDENCE_REQUEST_SENT: "Recover evidence",
  EVIDENCE_RECEIVED: "Recover evidence",
  EVIDENCE_REJECTED: "Recover evidence",
  EVIDENCE_REQUEST_SATISFIED: "Recover evidence",
  INVALIDATION_PLAN_CREATED: "Re-verify",
  INVALIDATION_PLAN_APPLIED: "Re-verify",
  PENDING_DECISION_CREATED: "Human decision",
  HUMAN_DECISION_RECORDED: "Human decision",
  HUMAN_DECISION_APPLIED: "Human decision",
  CASE_FINALIZED: "Complete",
};


/** Audit summaries are written for the record, so they carry state names verbatim: "BX-210:
 *  WAITING_FOR_EVIDENCE → ACCEPTED". That is right for the stored event and wrong for a person reading a
 *  timeline. This softens the shouting for display only, the underlying summary is untouched, and
 *  identifiers (BX-210, LIMS-EXP-0210) are left exactly as they are, because those are the strings someone
 *  will search for. */
const KEEP_UPPERCASE = new Set(["LIMS", "QA", "PI", "CSV", "JSON", "ID"]);

function humanise(summary: string): string {
  // The lookarounds exclude anything touching a hyphen or another word character, so a run inside an
  // identifier is never touched: LIMS-EXP-0210 stays LIMS-EXP-0210. A \b boundary is not enough; it sits
  // happily either side of a hyphen, which is how EXP got lowercased the first time I wrote this.
  return summary.replace(/(?<![\w-])[A-Z][A-Z_]{2,}(?![\w-])/g, (token) =>
    KEEP_UPPERCASE.has(token) ? token : token.replaceAll("_", " ").toLowerCase(),
  );
}


/** Who actually did this, for display.
 *
 *  The stored actor on an agent-driven run is SYSTEM, because a system trigger started the invocation and
 *  that is the honest answer for the record. But labelling every row of the timeline "system" hides the
 *  thing a reader most wants to know, which is whether a person or the agent produced it. An event that
 *  names a tool was produced by the agent calling that tool, so the label is derived rather than assumed,
 *  and the underlying audit record is left exactly as written. */
function actorLabel(e: AuditEvent): { label: string; cls: string } {
  if (e.actor_type === "HUMAN") return { label: "person", cls: "text-decision-fg" };
  if (e.actor_type === "SENDER") return { label: "sender", cls: "text-warn-fg" };
  if (e.tool_name) return { label: "agent", cls: "text-accent" };
  return { label: "system", cls: "text-fg-subtle" };
}

type Section = "overview" | "evidence" | "exceptions" | "activity" | "report";

function phaseOf(e: AuditEvent): string {
  return PHASE_OF[e.event_type] ?? (e.kind === "DOMAIN_EFFECT" ? "Decide" : "Agent internals");
}

export function CaseWorkspace({ caseId }: { caseId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const autoRun = params.get("run") === "1";
  const [view, setView] = useState<CaseView | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [outbox, setOutbox] = useState<OutboxMessage[]>([]);
  const [decisions, setDecisions] = useState<PendingDecision[]>([]);
  const [agentRunning, setAgentRunning] = useState(false);
  const [temps, setTemps] = useState<TemperatureSeries | null>(null);
  const [tab, setTab] = useState<Section>("overview");
  const [showTools, setShowTools] = useState(false);
  const [running, setRunning] = useState(false);
  const [last, setLast] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ sample: string; category: string } | null>(null);
  const seq = useRef(0);
  const autoRan = useRef(false);

  /** Cheap poll: only new audit events + the "is the agent working" flag. */
  const tick = useCallback(async () => {
    try {
      const page = await api.events(caseId, seq.current);
      setAgentRunning(page.agent_running);
      if (page.events.length) {
        seq.current = page.events[page.events.length - 1].sequence;
        setEvents((prev) => [...prev, ...page.events]);
        return true; // something changed → refresh the heavier views
      }
      return false;
    } catch (e) {
      setError(String(e));
      return false;
    }
  }, [caseId]);

  /** Expensive refresh: full snapshot/report/outbox/decisions. */
  const refreshAll = useCallback(async () => {
    try {
      const [v, ob, dec, tp] = await Promise.all([
        api.getCase(caseId),
        api.outbox(caseId),
        api.decisions(caseId),
        api.temperature(caseId).catch(() => null),
      ]);
      setView(v);
      setTemps(tp);
      setOutbox(ob);
      setDecisions(dec);
      setAgentRunning(v.agent_running);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [caseId]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const loop = async () => {
      const changed = await tick();
      if (changed) await refreshAll();
      if (!stop) timer = setTimeout(loop, pollInterval(agentRunning || running));
    };
    const first = setTimeout(async () => {
      await refreshAll();
      loop();
    }, 0);
    return () => {
      stop = true;
      clearTimeout(first);
      clearTimeout(timer);
    };
  }, [tick, refreshAll, agentRunning, running]);

  const runAgent = useCallback(async () => {
    setRunning(true);
    setError(null);
    setTab("activity"); // show the work, not just the result
    try {
      setLast(await api.run(caseId));
      await refreshAll();
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  }, [caseId, refreshAll]);

  useEffect(() => {
    if (!autoRun || autoRan.current) return;
    autoRan.current = true;
    router.replace(`/cases/${caseId}`); // drop ?run=1 so a refresh doesn't re-run
    const t = setTimeout(() => void runAgent(), 50);
    return () => clearTimeout(t);
  }, [autoRun, runAgent, router, caseId]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    view?.snapshot.samples.forEach((s) => (c[s.state] = (c[s.state] ?? 0) + 1));
    return c;
  }, [view]);
  const checkIndex = useMemo(() => {
    const m = new Map<string, CheckDetail>();
    view?.checks.forEach((c) => m.set(`${c.sample_id}:${c.category}`, c));
    return m;
  }, [view]);
  const focused = focus ? checkIndex.get(`${focus.sample}:${focus.category}`) : undefined;
  const checksDone = view?.checks.length ?? 0;
  const pending = decisions.filter((d) => !d.resolved_decision_id);

  /** Discrepancies are a first-class object in real receiving software (BSI: Add/Resolve/Mark irresolvable),
   *  with three states. Ours are derived from the check results rather than stored separately:
   *   unresolved  , a required check is not PASS and the sample is still open
   *   resolved    , the check now passes but the case recorded it as a problem earlier
   *   irresolvable; the check will never pass; the sample was dispositioned instead (BSI's own state) */
  const discrepancies = useMemo(() => {
    const terminal = new Set(["ACCEPTED", "ACCEPTED_WITH_EXCEPTION", "QUARANTINED", "REJECTED"]);
    const rows: { sample: string; category: string; status: string; reasons: string[]; observed: string | null; state: "unresolved" | "resolved" | "irresolvable" }[] = [];
    const everFlagged = new Set(
      events
        .filter((e) => e.event_type === "CHECK_RECORDED")
        .flatMap((e) => Object.entries((e.metadata?.results ?? {}) as Record<string, string>))
        .filter(([, v]) => v !== "PASS")
        .map(([k]) => k),
    );
    for (const s of view?.snapshot.samples ?? []) {
      for (const c of CHECKS) {
        const key = `${s.sample_id}:${c}`;
        const detail = checkIndex.get(key);
        const now = s.checks[c];
        if (!detail) continue;
        const isProblem = now !== "PASS";
        if (!isProblem && !everFlagged.has(key)) continue;
        rows.push({
          sample: s.sample_id,
          category: c,
          status: now ?? "-",
          reasons: detail.reason_codes,
          observed: detail.observed_value,
          state: !isProblem ? "resolved" : terminal.has(s.state) ? "irresolvable" : "unresolved",
        });
      }
    }
    return rows;
  }, [view, events, checkIndex]);
  const discrepancyCounts = useMemo(
    () => ({
      unresolved: discrepancies.filter((d) => d.state === "unresolved").length,
      resolved: discrepancies.filter((d) => d.state === "resolved").length,
      irresolvable: discrepancies.filter((d) => d.state === "irresolvable").length,
    }),
    [discrepancies],
  );
  const busy = agentRunning || running;

  if (!view) {
    return (
      <>
        <PageHeader title={caseId} meta={error ? undefined : "Loading the case…"} />
        {error ? (
          <PageError error={error} onRetry={refreshAll} backHref="/" backLabel="Back to the queue" />
        ) : (
          <div className="w-full space-y-4 p-4 sm:p-5">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-80 w-full" />
          </div>
        )}
      </>
    );
  }
  const { snapshot, report } = view;

  const nav: { id: Section; label: string; badge?: number; tone?: "decision" | "warn" }[] = [
    { id: "overview", label: "Overview" },
    { id: "evidence", label: "Evidence" },
    {
      id: "exceptions",
      label: "Exceptions",
      badge:
        discrepancyCounts.unresolved + pending.length + outbox.filter((m) => m.status === "ACTIVE").length,
      tone: pending.length ? "decision" : "warn",
    },
    { id: "activity", label: "Activity" },
    { id: "report", label: "Report" },
  ];

  return (
    <>
      <PageHeader
        title={report.shipment_id}
        badge={<StateBadge state={snapshot.state} />}
        meta={
          <>
            {report.protocol} · policy {report.policy} · {snapshot.samples.length} specimens · case v
            {snapshot.case_version}
            {report.received_at && (
              <>
                {" · received "}
                <Elapsed since={report.received_at} warnAfterMs={4 * 3600_000} />
              </>
            )}
          </>
        }
        actions={
          <>
            <AgentPulse running={busy} />
            {snapshot.state === "VERIFYING" && (
              <Button onClick={runAgent} disabled={running}>
                {running ? "Agent running…" : "Run agent"}
              </Button>
            )}
          </>
        }
      />

      {/* Sidebar plus a single working pane. One section is on screen at a time, which is what keeps this
          page from becoming the endless scroll it used to be, the summary a coordinator glances at stays
          pinned in the sidebar instead of scrolling away above the thing they are reading. */}
      <div className="grid min-h-0 grow lg:grid-cols-[13.5rem_minmax(0,1fr)]">
        <aside className="shrink-0 border-b border-border bg-surface-2/60 p-4 lg:sticky lg:top-[3.75rem] lg:h-[calc(100dvh-3.75rem)] lg:overflow-y-auto lg:border-b-0 lg:border-r">
          <DispositionSummary counts={counts} total={snapshot.samples.length} checksDone={checksDone} />

          {/* Below lg the sidebar becomes a band above the content and this turns into a row. Without the
              scroller the five items simply overflow the screen on a narrow one, taking the badges with
              them; the counts are the part a coordinator is looking for. */}
          <nav
            aria-label="Case sections"
            className="-mx-1 mt-4 flex gap-1 overflow-x-auto px-1 lg:mx-0 lg:flex-col lg:overflow-visible lg:px-0"
          >
            {nav.map((n) => (
              <button
                key={n.id}
                onClick={() => setTab(n.id)}
                aria-current={tab === n.id ? "page" : undefined}
                className={`relative flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition ${
                  tab === n.id
                    ? "bg-surface font-semibold text-fg before:absolute before:top-1 before:bottom-1 before:left-0 before:w-0.5 before:rounded-r before:bg-accent"
                    : "text-fg-muted hover:bg-surface/60 hover:text-fg"
                }`}
              >
                {n.label}
                {n.badge ? (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[12px] font-bold tabular-nums ${
                      tab === n.id
                        ? "bg-accent-fg/20 text-accent-fg"
                        : n.tone === "decision"
                          ? "bg-decision-fg text-decision-bg"
                          : "bg-warn-fg text-warn-bg"
                    }`}
                  >
                    {n.badge}
                  </span>
                ) : null}
              </button>
            ))}
          </nav>

          {pending.length > 0 && (
            <div className="mt-4 rounded-lg border border-decision-border bg-decision-bg p-3">
              <p className="text-sm font-semibold text-decision-fg">Your decision is required</p>
              <p className="mt-1 text-[13px] leading-snug text-decision-fg/90">
                {pending.map((p) => p.sample_id).join(", ")}, the agent verified everything else and cannot
                decide this under the protocol.
              </p>
              {pending[0].interrupt_id && (
                <Link
                  href={`/cases/${caseId}/decide/${encodeURIComponent(pending[0].interrupt_id)}`}
                  className="mt-2 block rounded-md bg-decision-fg px-3 py-1.5 text-center text-sm font-semibold text-decision-bg hover:brightness-110"
                >
                  Open decision card
                </Link>
              )}
            </div>
          )}
        </aside>

        <div className="min-w-0 space-y-4 p-4 sm:p-5">
          <ErrorBox error={error} onRetry={refreshAll} />
          {last && <RunResultCard r={last} />}

          {tab === "overview" && (
            <div className="space-y-4">
              {/* Not items-start: the receipt is shorter than the cold chain and the two cards
                  ended at different heights, which reads as a layout that gave up rather than one
                  that was arranged. */}
              <div className="grid gap-4 xl:grid-cols-2">
                <ReceiptRecord report={report} snapshot={snapshot} />
                <ColdChain temps={temps} />
              </div>
              <RecentActivity events={events} onSeeAll={() => setTab("activity")} />
            </div>
          )}

          {tab === "evidence" && (
            <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
              <Card
                title="Evidence matrix"
                right={
                  <span className="hidden text-[13px] text-fg-subtle sm:inline">
                    ✓ pass · ✗ fail · … awaiting · ? ambiguous · ~ provisional, click any cell
                  </span>
                }
              >
                <div className="-mx-4 overflow-x-auto px-4">
                  <table className="w-full min-w-[56rem] table-fixed text-sm">
                    <colgroup>
                      <col className="w-[6.5rem]" />
                      {CHECKS.map((c) => (
                        <col key={c} className="w-[6.25rem]" />
                      ))}
                      <col />
                    </colgroup>
                    <thead className="text-left text-fg-subtle">
                      <tr className="border-b border-border">
                        <th scope="col" className="eyebrow pb-2 font-semibold">Sample</th>
                        {CHECKS.map((c) => (
                          <th key={c} scope="col" className="eyebrow whitespace-nowrap pb-2 text-center font-semibold">
                            {CHECK_LABELS[c]}
                          </th>
                        ))}
                        <th scope="col" className="eyebrow pb-2 pl-3 font-semibold">Disposition</th>
                      </tr>
                    </thead>
                    <tbody>
                      {snapshot.samples.map((s) => (
                        <tr
                          key={s.sample_id}
                          className={`border-t border-border ${focus?.sample === s.sample_id ? "bg-surface-2" : ""}`}
                        >
                          <th scope="row" className="py-1.5 pr-2 text-left font-mono font-medium">{s.sample_id}</th>
                          {CHECKS.map((c) => {
                            const detail = checkIndex.get(`${s.sample_id}:${c}`);
                            return (
                              <td key={c} className="py-1.5 text-center">
                                <CheckCell
                                  status={s.checks[c]}
                                  label={`${s.sample_id} ${CHECK_LABELS[c]}`}
                                  active={focus?.sample === s.sample_id && focus?.category === c}
                                  onClick={
                                    detail
                                      ? () =>
                                          setFocus(
                                            focus?.sample === s.sample_id && focus?.category === c
                                              ? null
                                              : { sample: s.sample_id, category: c },
                                          )
                                      : undefined
                                  }
                                  provisional={detail?.provisional}
                                />
                              </td>
                            );
                          })}
                          <td className="py-1.5 pl-3"><StateBadge state={s.state} small /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {focused && <EvidenceDetail detail={focused} onClose={() => setFocus(null)} />}
              </Card>
              <Discrepancies rows={discrepancies} counts={discrepancyCounts} onFocus={setFocus} />
            </div>
          )}

          {tab === "exceptions" && (
            <div className="grid items-start gap-4 xl:grid-cols-2">
              <Discrepancies rows={discrepancies} counts={discrepancyCounts} onFocus={setFocus} />
              <Outbox outbox={outbox} />
            </div>
          )}

          {tab === "activity" && (
            <ActivityTimeline events={events} showTools={showTools} onToggleTools={setShowTools} busy={busy} />
          )}

          {tab === "report" && (
            <ReportView
              report={report}
              caseId={caseId}
              onChanged={async () => {
                await refreshAll();
                // Reopening a hold exists to get a fresh answer, so ask for one rather than leaving the
                // specimen sitting in PENDING behind a button the reviewer has to go and find.
                await runAgent();
              }}
            />
          )}
        </div>
      </div>
    </>
  );
}

/** The summary a coordinator glances at, pinned so it never scrolls away from what they are reading. */
function DispositionSummary({
  counts,
  total,
  checksDone,
}: {
  counts: Record<string, number>;
  total: number;
  checksDone: number;
}) {
  const rows = [
    ["ACCEPTED", "Accepted", "text-pass-fg"],
    ["WAITING_FOR_EVIDENCE", "Waiting on sender", "text-warn-fg"],
    ["NEEDS_HUMAN_DECISION", "Needs a decision", "text-decision-fg"],
    ["QUARANTINED", "On hold", "text-fail-fg"],
    ["REJECTED", "Rejected", "text-fail-fg"],
  ] as const;
  return (
    <div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-3xl font-semibold tabular-nums leading-none text-fg">{total}</span>
        <span className="eyebrow">specimens</span>
      </div>
      <div className="mt-2">
        <DispositionBar counts={counts} showLegend={false} />
      </div>
      <dl className="mt-3 space-y-1">
        {rows
          .filter(([k]) => (counts[k] ?? 0) > 0)
          .map(([k, label, tone]) => (
            <div key={k} className="flex items-baseline justify-between gap-2">
              <dt className="text-sm text-fg-muted">{label}</dt>
              <dd className={`text-sm font-bold tabular-nums ${tone}`}>{counts[k]}</dd>
            </div>
          ))}
      </dl>
      <div className="mt-3">
        <CheckProgress done={checksDone} total={total * CHECKS.length} />
      </div>
    </div>
  );
}


/** The last few things that actually happened, in the order they happened, on the page a coordinator opens
 *  first. The full trail is a click away; this is here so "what is going on with this shipment" is answered
 *  without navigating anywhere. */
function RecentActivity({ events, onSeeAll }: { events: AuditEvent[]; onSeeAll: () => void }) {
  const recent = events.filter((e) => e.kind === "DOMAIN_EFFECT").slice(-8).reverse();
  if (recent.length === 0) return null;
  return (
    <Card
      title="What just happened"
      right={
        <button onClick={onSeeAll} className="text-sm text-accent hover:underline">
          Full trail →
        </button>
      }
    >
      <ol className="space-y-0">
        {recent.map((e) => (
          <li key={e.audit_event_id} className="flex gap-3 border-b border-border/50 py-1 last:border-0">
            <span className="w-14 shrink-0 font-mono text-[13px] leading-5 text-fg-subtle">
              {new Date(e.timestamp).toLocaleTimeString([], { hour12: false })}
            </span>
            <span className={`w-14 shrink-0 text-[12px] font-semibold uppercase leading-5 tracking-wide ${actorLabel(e).cls}`}>
              {actorLabel(e).label}
            </span>
            <span className="min-w-0 grow text-sm leading-5 text-fg">{humanise(e.summary)}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}

type Discrepancy = {
  sample: string;
  category: string;
  status: string;
  reasons: string[];
  observed: string | null;
  state: "unresolved" | "resolved" | "irresolvable";
};

/* A left border carries the state rather than a filled background. Benchling's results tables do the same,
   and the reason is practical: a 2px rule survives a row that already has seven status glyphs on it, where
   a wash of colour would fight them. */
const DISCREPANCY_STYLE: Record<Discrepancy["state"], string> = {
  // Ordered by how much they want attention. Irresolvable was drawn with a pale rule that read louder than
  // unresolved, which inverted the whole point of the list.
  unresolved: "border-l-fail-fg bg-fail-bg/40",
  irresolvable: "border-l-fg-subtle bg-surface-2/60",
  resolved: "border-l-pass-fg/60 bg-transparent",
};

function Discrepancies({
  rows,
  counts,
  onFocus,
}: {
  rows: Discrepancy[];
  counts: { unresolved: number; resolved: number; irresolvable: number };
  onFocus: (f: { sample: string; category: string }) => void;
}) {
  return (
    <Card
      title="Discrepancies"
      right={
        // Two numbers, not three. "Resolved" and "irresolvable" are both closed as far as this panel's
        // header is concerned; which of the two a given row is belongs on the row, and it is there.
        <span className="shrink-0 whitespace-nowrap text-[13px] text-fg-subtle tabular-nums">
          {counts.unresolved} open · {counts.resolved + counts.irresolvable} closed
        </span>
      }
    >
      {rows.length === 0 ? (
        <EmptyState title="No discrepancies." hint="Every required check passed on first evaluation." />
      ) : (
        <ul className="space-y-1.5 text-sm">
          {rows.map((d) => (
            <li key={d.sample + d.category}>
              <button
                onClick={() => onFocus({ sample: d.sample, category: d.category })}
                className={`w-full border-l-4 border-y border-r border-border px-3 py-2 text-left transition hover:border-border-strong ${DISCREPANCY_STYLE[d.state]}`}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="font-mono font-semibold">{d.sample}</span>
                  <span className="text-[12px] font-semibold uppercase tracking-wider text-fg-subtle">{d.state}</span>
                </span>
                <span className="mt-0.5 block text-sm text-fg-muted">
                  {CHECK_LABELS[d.category]} · {d.status}
                  {d.observed ? `, ${d.observed}` : ""}
                </span>
                {d.reasons.length > 0 && (
                  <span className="mt-1 block font-mono text-[12px] text-fg-subtle">{d.reasons.join(", ")}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Outbox({ outbox }: { outbox: OutboxMessage[] }) {
  return (
    <Card title="Evidence requests (demo outbox)">
      {outbox.length === 0 ? (
        <EmptyState
          title="Nothing sent yet."
          hint="If the agent finds recoverable gaps it writes one consolidated request here."
        />
      ) : (
        <ul className="space-y-3">
          {outbox.map((m) => (
            <li key={m.request_id} className="rounded border border-border bg-surface-2 p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-mono text-sm">{m.request_id}</span>
                <StateBadge state={m.status} small />
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 text-sm text-fg-muted">
                <span>
                  To {m.to.display_name} · {m.to.destination}
                </span>
                <span aria-hidden>·</span>
                <span className={ageOf(m.sent_at).bucket.cls}>
                  sent <Elapsed since={m.sent_at} warnAfterMs={DISCREPANCY_SLA_HOURS * 3600_000} />
                  {m.status === "ACTIVE" && ` · ${ageOf(m.sent_at).bucket.label}`}
                </span>
              </div>
              <div className="mt-1 font-medium">{m.subject}</div>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-surface p-2 text-[13px] text-fg-muted">
                {m.body}
              </pre>
              {m.status === "ACTIVE" && (
                <Link
                  href={m.portal_path}
                  className="mt-2 inline-block rounded bg-warn-fg px-3 py-1 text-sm font-medium text-warn-bg hover:brightness-110"
                >
                  Open the sender&apos;s secure link →
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function EvidenceDetail({ detail, onClose }: { detail: CheckDetail; onClose: () => void }) {
  return (
    <aside className="mt-4 rounded border border-info-border bg-info-bg p-3 text-sm" aria-live="polite">
      <div className="flex items-start justify-between gap-3">
        <div>
          <span className="font-mono">{detail.sample_id}</span> · {CHECK_LABELS[detail.category] ?? detail.category} ·{" "}
          <span className="font-mono text-sm">{detail.status}</span>
          {detail.provisional && <span className="ml-2 rounded bg-warn-bg px-1.5 py-0.5 text-[12px] text-warn-fg">provisional</span>}
        </div>
        <button onClick={onClose} aria-label="Close evidence detail" className="text-sm text-fg-muted hover:text-fg">
          ✕
        </button>
      </div>
      <p className="mt-2 text-fg">{detail.summary}</p>
      <dl className="mt-2 grid grid-cols-[110px_1fr] gap-y-1 text-sm">
        {detail.observed_value && (
          <>
            <dt className="text-fg-subtle">Observed</dt>
            <dd className="font-mono text-fg-muted">{detail.observed_value}</dd>
          </>
        )}
        {detail.expected_value && (
          <>
            <dt className="text-fg-subtle">Expected</dt>
            <dd className="font-mono text-fg-muted">{detail.expected_value}</dd>
          </>
        )}
        {detail.reason_codes.length > 0 && (
          <>
            <dt className="text-fg-subtle">Reason</dt>
            <dd className="font-mono text-fg-muted">{detail.reason_codes.join(", ")}</dd>
          </>
        )}
        <dt className="text-fg-subtle">Evidence</dt>
        <dd className="font-mono text-[12px] text-fg-muted">{detail.evidence_refs.join(" ") || "-"}</dd>
        <dt className="text-fg-subtle">Rule</dt>
        <dd className="font-mono text-[12px] text-fg-muted">{detail.rule_version}</dd>
      </dl>
    </aside>
  );
}

function ActivityTimeline({
  events,
  showTools,
  onToggleTools,
  busy,
}: {
  events: AuditEvent[];
  showTools: boolean;
  onToggleTools: (v: boolean) => void;
  busy: boolean;
}) {
  const visible = events.filter((e) => e.kind === "DOMAIN_EFFECT" || (showTools && e.kind !== "DOMAIN_EFFECT"));
  const groups: { phase: string; items: AuditEvent[] }[] = [];
  for (const e of visible) {
    const phase = phaseOf(e);
    const last = groups[groups.length - 1];
    if (last && last.phase === phase) last.items.push(e);
    else groups.push({ phase, items: [e] });
  }
  return (
    <Card
      title="Agent activity"
      right={
        <div className="flex items-center gap-3">
          <AgentPulse running={busy} />
          <label className="flex items-center gap-2 text-sm text-fg-muted">
            <input type="checkbox" checked={showTools} onChange={(e) => onToggleTools(e.target.checked)} /> show tool calls
          </label>
        </div>
      }
    >
      {groups.length === 0 ? (
        <EmptyState title="No activity yet." hint="Run the agent to see what it does, step by step." />
      ) : (
        <div className="space-y-4">
          {groups.map((g, gi) => (
            <section key={gi}>
              <h3 className="mb-1 flex items-center gap-2 text-[13px] uppercase tracking-wider text-accent">
                <span className="h-px w-4 bg-accent" />
                {g.phase}
                <span className="text-fg-subtle">({g.items.length})</span>
              </h3>
              <ol className="space-y-0.5 border-l border-border pl-3 font-mono text-sm">
                {g.items.map((e) => (
                  <li
                    key={e.audit_event_id}
                    // Not flex-wrap: a long summary wrapped the whole row and restarted at the left
                    // margin, so the columns stopped lining up exactly where the interesting events
                    // are. The summary wraps inside its own column instead.
                    className={`flex gap-x-3 rounded px-2 py-1 ${e.kind === "DOMAIN_EFFECT" ? "text-fg" : "text-fg-subtle"} ${e.event_type === "INTERVENTION_DENIED" || e.output_status === "rejected" ? "bg-fail-bg" : ""}`}
                  >
                    <span className="w-9 shrink-0 text-right text-fg-subtle">{e.sequence}</span>
                    {/* w-14 is narrower than "19:27:26" in this face, so the time ran into the label. */}
                    <span className="w-[4.75rem] shrink-0 text-fg-subtle">
                      {new Date(e.timestamp).toLocaleTimeString([], { hour12: false })}
                    </span>
                    {/* Fixed width and shrink-0 without truncate let a long unlabelled event type
                        run straight into the summary beside it. Labelling them is the fix; this is
                        the guard so a new one can never do it again. */}
                    <span
                      className="w-40 shrink-0 truncate text-fg-muted"
                      title={DOMAIN_LABELS[e.event_type] ? e.event_type : undefined}
                    >
                      {DOMAIN_LABELS[e.event_type] ?? e.event_type}
                    </span>
                    <span className="min-w-0 grow break-words">{humanise(e.summary)}</span>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      )}
    </Card>
  );
}


/* ------------------------------------------------------------------------------------------------
   Specimens on hold.

   Quarantine is where the system puts what it could not settle, which makes it the one place a
   product like this quietly rots. Held material sits, nobody owns it, and a year later nobody
   remembers why. So the hold is visible, it is attributed, and it has a way out: reopening it puts the
   specimen back through verification. Note what this control does NOT do; it cannot accept anything.
   The reviewer reopens the question; the policy engine still answers it.
------------------------------------------------------------------------------------------------ */
function HeldSpecimens({
  report,
  caseId,
  onChanged,
}: {
  report: CaseView["report"];
  caseId: string;
  onChanged: () => Promise<void> | void;
}) {
  const held = report.samples.filter((s) => s.state === "QUARANTINED");
  const [open, setOpen] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  if (!held.length) return null;

  async function reopen(sampleId: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await api.reopenQuarantine(caseId, sampleId, reason);
      setOutcome(`${sampleId}: ${r.summary}`);
      setOpen(null);
      setReason("");
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={`On hold (${held.length})`}>
      <p className="mb-3 text-sm text-fg-muted">
        These specimens were held rather than accepted or rejected. Reopening a hold re-runs verification
        against whatever evidence now exists; it does not accept the specimen, and the engine may hold it
        again.
      </p>
      {outcome && <div className="mb-3 rounded border border-info-border bg-info-bg px-3 py-2 text-sm text-info-fg">{outcome}</div>}
      <ul className="space-y-2">
        {held.map((s) => (
          <li key={s.sample_id} className="rounded border border-border bg-surface-2 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-3">
                <span className="font-mono text-sm text-fg">{s.sample_id}</span>
                <StateBadge state={s.state} small />
                {s.lims && <span className="font-mono text-sm text-fg-subtle">{s.lims.record_id}</span>}
              </div>
              {open === s.sample_id ? (
                <button onClick={() => setOpen(null)} className="text-sm text-fg-muted hover:text-fg">
                  cancel
                </button>
              ) : (
                <Button variant="ghost" onClick={() => setOpen(s.sample_id)}>
                  Reopen this hold
                </Button>
              )}
            </div>
            {open === s.sample_id && (
              <div className="mt-3">
                <label className="text-sm text-fg-muted">
                  Why is this being reopened?
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="e.g. site sent the missing consent addendum"
                    className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
                  />
                </label>
                <div className="mt-2 flex items-center gap-2">
                  <Button onClick={() => reopen(s.sample_id)} disabled={busy || !reason.trim()}>
                    {busy ? "Re-verifying…" : "Reopen and re-verify"}
                  </Button>
                  <span className="text-sm text-fg-subtle">Recorded against your name in the audit log.</span>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
      <ErrorBox error={error} />
    </Card>
  );
}

function ReportView({ report, caseId, onChanged }: { report: CaseView["report"]; caseId: string; onChanged: () => Promise<void> | void }) {
  const download = () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${report.case_id}-intake-report.json`;
    a.click();
  };
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-5">
        {[
          ["Accepted", report.counts.ACCEPTED],
          ["Quarantined", report.counts.QUARANTINED],
          ["Evidence requests", report.evidence_requests.length],
          ["Human decisions", report.human_decisions.length],
          ["Unauthorized acceptances", report.unauthorized_acceptances],
        ].map(([k, v]) => (
          <div key={String(k)} className="rounded border border-border bg-surface-2 px-3 py-2">
            <div className={`text-2xl font-semibold ${k === "Unauthorized acceptances" ? (v === 0 ? "text-pass-fg" : "text-fail-fg") : ""}`}>{v}</div>
            <div className="text-[13px] uppercase tracking-wider text-fg-subtle">{k}</div>
          </div>
        ))}
      </div>
      <Card title="Per-sample disposition" right={<Button variant="ghost" onClick={download}>Download JSON</Button>}>
        <div className="-mx-2 overflow-x-auto px-2">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-left text-[13px] uppercase tracking-wider text-fg-subtle">
              <tr>
                <th scope="col" className="pb-2">Sample</th>
                <th scope="col" className="pb-2">State</th>
                <th scope="col" className="pb-2">LIMS record</th>
                <th scope="col" className="pb-2">Policy evaluation</th>
                <th scope="col" className="pb-2">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {report.samples.map((s) => (
                <tr key={s.sample_id} className="border-t border-border">
                  <th scope="row" className="py-1 text-left font-mono font-normal">{s.sample_id}</th>
                  <td className="py-1">
                    <StateBadge state={s.state} small />
                  </td>
                  <td className="py-1 font-mono text-sm">{s.lims ? `${s.lims.record_id} · ${s.lims.status}` : "-"}</td>
                  <td className="py-1 font-mono text-sm text-fg-muted">{s.lims?.policy_evaluation_id ?? "-"}</td>
                  <td className="py-1 font-mono text-[12px] text-fg-subtle">{s.evidence_refs.join(" ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <VerificationReportView caseId={caseId} />
      <HeldSpecimens report={report} caseId={caseId} onChanged={onChanged} />
      {report.human_decisions.length > 0 && (
        <Card title="Human decisions">
          {report.human_decisions.map((d) => (
            <div key={d.decision_id} className="text-sm">
              <span className="font-mono">{d.sample_id}</span>, <span className="font-semibold">{d.selected_option}</span> by {d.actor_id} ({d.actor_role}) · {d.comment}
            </div>
          ))}
        </Card>
      )}
      <Card title="Audit trail">
        <ol className="max-h-96 space-y-0.5 overflow-auto font-mono text-[13px]">
          {report.audit_events
            .filter((e) => e.kind === "DOMAIN_EFFECT")
            .map((e) => (
              <li key={e.seq} className="flex flex-wrap gap-x-3">
                <span className="w-8 shrink-0 text-right text-fg-subtle">{e.seq}</span>
                <span className="w-52 shrink-0 text-fg-subtle">{e.type}</span>
                <span className="min-w-0 grow break-words text-fg-muted">{e.summary}</span>
              </li>
            ))}
        </ol>
        <div className="mt-2 text-[13px] text-fg-subtle">
          {report.audit_counts_by_kind.DOMAIN_EFFECT} domain effects · {report.audit_counts_by_kind.TOOL_ATTEMPT} tool attempts · {report.audit_counts_by_kind.TELEMETRY} telemetry
        </div>
      </Card>
    </div>
  );
}

/** The receipt record. ISO/TS 20658 §17.4 makes date+time of receipt, the identity of the receiver and the
 *  condition (with the reason when unacceptable) mandatory fields; ISO 20387 Annex A.3 adds the transport
 *  temperature and the temperature at reception. ISO 20387 §7.3.2.4 requires received material be segregated
 *  "to prevent final storage until legal, ethical, documentation and quality compliance has been assessed",
 *  which is why nothing here has a permanent storage location yet. */
function ReceiptRecord({ report, snapshot }: { report: CaseView["report"]; snapshot: CaseView["snapshot"] }) {
  const scanned = snapshot.samples.length;
  const settled = snapshot.samples.filter((s) => ["ACCEPTED", "ACCEPTED_WITH_EXCEPTION", "QUARANTINED", "REJECTED"].includes(s.state)).length;
  return (
    <Card title="Receipt record">
      <dl className="grid grid-cols-[130px_1fr] gap-y-1.5 text-sm">
        <dt className="text-fg-subtle">Received</dt>
        <dd>
          {new Date(report.received_at).toLocaleString()} · <Elapsed since={report.received_at} warnAfterMs={4 * 3600_000} />
        </dd>
        <dt className="text-fg-subtle">Shipment</dt>
        <dd className="font-mono">{report.shipment_id}</dd>
        <dt className="text-fg-subtle">Protocol</dt>
        <dd className="font-mono">{report.protocol}</dd>
        <dt className="text-fg-subtle">Specimens</dt>
        <dd>
          {scanned} scanned of {scanned} expected · {settled} dispositioned
        </dd>
        <dt className="text-fg-subtle">Storage</dt>
        <dd>
          <span className="rounded bg-warn-bg px-1.5 py-0.5 text-sm text-warn-fg">quarantine storage</span>{" "}
          <span className="text-sm text-fg-muted">held pending disposition, no permanent location assigned</span>
        </dd>
      </dl>
      <p className="mt-3 border-t border-border pt-2 text-[13px] leading-relaxed text-fg-subtle">
        Received material is segregated to prevent final storage until documentation and quality compliance has been
        assessed (ISO 20387 §7.3.2.4; ISBER §J6 designates a quarantine storage unit for exactly this).
      </p>
    </Card>
  );
}

/** Cold chain is read inside the receiving inspection pass, before the manifest is reconciled, so it belongs
 *  at the top of the case, not seventh in a row of seven checks (ISBER §J6, GTEx OP-0011 §8.5.3). Practice
 *  evaluates peak, cumulative and longest-continuous independently. */
function ColdChain({ temps }: { temps: TemperatureSeries | null }) {
  if (!temps || temps.loggers.length === 0) {
    return (
      <Card title="Cold chain">
        <EmptyState title="No transport logger data." hint="A shipment without a logger cannot evidence its temperature history." />
      </Card>
    );
  }
  return (
    <Card
      title="Cold chain"
      right={
        <span className="text-[13px] text-fg-subtle">
          permitted {temps.permitted.min_c}–{temps.permitted.max_c} °C · tolerance {temps.permitted.tolerance_minutes} min
        </span>
      }
    >
      <div className="space-y-4">
        {temps.loggers.map((lg) => {
          const t0 = new Date(lg.series[0]?.t ?? 0).getTime();
          const points = lg.series.map((p) => ({ t: (new Date(p.t).getTime() - t0) / 60000, c: p.c }));
          const failed = lg.status !== "PASS";
          return (
            <div key={lg.logger_id}>
              <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="font-mono">{lg.logger_id}</span>
                <span className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 font-mono text-[12px] ${failed ? "bg-fail-bg text-fail-fg" : "bg-pass-bg text-pass-fg"}`}>
                    {failed ? "EXCURSION" : "WITHIN RANGE"}
                  </span>
                  <span className="text-fg-subtle">{lg.reading_count.toLocaleString()} readings</span>
                </span>
              </div>
              <TemperatureTrace
                points={points}
                min={temps.permitted.min_c}
                max={temps.permitted.max_c}
                height={80}
                caption={false}
                label={`${lg.logger_id}: ${lg.summary}`}
              />
              <dl className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-fg-muted">
                <span>
                  <dt className="inline text-fg-subtle">peak </dt>
                  <dd className={`inline font-mono ${failed ? "text-fail-fg" : ""}`}>{lg.metrics.peak_c?.toFixed(1)} °C</dd>
                </span>
                <span>
                  <dt className="inline text-fg-subtle">cumulative out </dt>
                  <dd className={`inline font-mono ${failed ? "text-fail-fg" : ""}`}>{lg.metrics.cumulative_minutes_out.toFixed(0)} min</dd>
                </span>
                <span>
                  <dt className="inline text-fg-subtle">longest run </dt>
                  <dd className="inline font-mono">{lg.metrics.longest_continuous_minutes.toFixed(0)} min</dd>
                </span>
              </dl>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
