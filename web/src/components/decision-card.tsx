"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api, CHECK_LABELS, type PendingDecision, type RunResult, type TemperatureSeries } from "@/lib/api";
import { Button, Card, ErrorBox, RunResultCard, Skeleton } from "@/components/ui";
import { PageHeader } from "@/components/shell";
import { TemperatureTrace } from "@/components/charts";

/** Attested vocabulary: CAP BAP.03000 uses "condition exception"/"condition warning"; ISO 9000 calls the
 *  permission a "concession". "Accepted with exception" appears in none of the standards. */
const OPTION_LABELS: Record<string, string> = {
  QUARANTINE: "Hold for review",
  APPROVE_EXCEPTION: "Accept with documented condition exception",
  REJECT: "Reject outright",
};

/** Rejection is the only choice here that cannot be walked back, so it is the only one styled as danger and
 *  the only one that asks twice. A hold is the cautious answer and should never look like the alarming one. */
const IRREVERSIBLE = new Set(["REJECT"]);

export function DecisionCard({ caseId, interruptId }: { caseId: string; interruptId: string }) {
  const [card, setCard] = useState<PendingDecision | null | undefined>(undefined);
  // A refused answer re-raises a FRESH interrupt (the decision is never consumed), so the id we must
  // respond to changes. Track the live id rather than the one in the URL.
  const [liveId, setLiveId] = useState(interruptId);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [refused, setRefused] = useState<{ option: string; result: RunResult } | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [temps, setTemps] = useState<TemperatureSeries | null>(null);


  useEffect(() => {
    const t = setTimeout(() => {
      api
        .decisions(caseId)
        .then((ds) => {
          const open = ds.find((d) => !d.resolved_decision_id && d.interrupt_id);
          const exact = ds.find((d) => d.interrupt_id === interruptId);
          const chosen = exact && !exact.resolved_decision_id ? exact : (open ?? exact ?? null);
          setCard(chosen);
          if (chosen?.interrupt_id) setLiveId(chosen.interrupt_id);
        })
        .catch((e) => setError(String(e)));
    }, 0);
    return () => clearTimeout(t);
  }, [caseId, interruptId]);

  useEffect(() => {
    if (!card || card.issue_type !== "TEMPERATURE_EXCURSION") return;
    const t = setTimeout(() => {
      api
        .temperature(caseId, card.sample_id)
        .then(setTemps)
        .catch(() => setTemps(null));
    }, 0);
    return () => clearTimeout(t);
  }, [caseId, card]);

  const trace = useMemo(() => {
    const lg = temps?.loggers[0];
    if (!lg || !temps) return null;
    const t0 = new Date(lg.series[0]?.t ?? 0).getTime();
    return {
      points: lg.series.map((p) => ({ t: (new Date(p.t).getTime() - t0) / 60000, c: p.c })),
      min: temps.permitted.min_c,
      max: temps.permitted.max_c,
      tolerance: temps.permitted.tolerance_minutes,
      logger: lg.logger_id,
      readings: lg.reading_count,
      metrics: lg.metrics,
    };
  }, [temps]);

  async function choose(option: string) {
    setBusy(option);
    setError(null);
    setRefused(null);
    try {
      const r = await api.respond(caseId, liveId, { selected_option: option, comment });
      // A fresh pending interrupt means the answer was refused: the card is still open under a NEW id.
      if (r.pending_interrupt) {
        setRefused({ option, result: r });
        setLiveId(r.pending_interrupt.interrupt_id);
        const ds = await api.decisions(caseId);
        setCard(ds.find((d) => d.interrupt_id === r.pending_interrupt!.interrupt_id) ?? card);
      } else {
        setResult(r);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <PageHeader
        title={card ? `${card.issue_type.replaceAll("_", " ").toLowerCase()}, ${card.sample_id}` : "Decision"}
        meta="The agent completed every check it is allowed to decide. The protocol reserves this one for a person."
        actions={
          <Link
            href={`/cases/${caseId}`}
            className="rounded-md border border-border bg-surface px-3 py-2 text-sm font-medium text-fg hover:bg-surface-2"
          >
            Back to the case
          </Link>
        }
      />
      {/* A single decision, centred and narrow. This is the one screen in the product where the person is
          meant to read every word before acting, so it is deliberately not a dense console. */}
      <div className="mx-auto w-full max-w-3xl space-y-4 p-4 sm:p-6">
        {card === undefined && (
          <div className="space-y-3">
            <Skeleton className="h-8 w-1/2" />
            <Skeleton className="h-48 w-full" />
          </div>
        )}
        {card === null && <p className="text-sm text-fg-muted">No pending decision with that id (it may already be resolved).</p>}
        {card && (
          <>
            <div className="sr-only">
              <h1>
                {card.issue_type.replaceAll("_", " ").toLowerCase()}, <span className="font-mono">{card.sample_id}</span>
              </h1>
              <p className="mt-1 text-sm text-fg-muted">The agent completed every check it is allowed to decide. The protocol reserves this one for a person.</p>
            </div>
            {trace && (
              <Card
                title="Transport temperature"
                right={
                  <span className="text-[13px] text-fg-subtle">
                    {trace.logger} · {trace.readings.toLocaleString()} readings
                  </span>
                }
              >
                <TemperatureTrace
                  points={trace.points}
                  min={trace.min}
                  max={trace.max}
                  label={`Transport temperature recorded by ${trace.logger} for ${card.sample_id}`}
                />
                {/* Practice evaluates three independent numbers; one alone mis-states an excursion. */}
                <dl className="mt-3 grid grid-cols-3 gap-3 border-t border-border pt-3 text-sm">
                  <div>
                    <dt className="text-sm uppercase tracking-wider text-fg-subtle">Peak</dt>
                    <dd className="font-mono text-lg text-fail-fg">{trace.metrics.peak_c?.toFixed(1)} °C</dd>
                    <dd className="text-[13px] text-fg-subtle">permitted ≤ {trace.max} °C</dd>
                  </div>
                  <div>
                    <dt className="text-sm uppercase tracking-wider text-fg-subtle">Cumulative out of range</dt>
                    <dd className="font-mono text-lg text-fail-fg">{trace.metrics.cumulative_minutes_out.toFixed(0)} min</dd>
                    <dd className="text-[13px] text-fg-subtle">tolerance {trace.tolerance} min</dd>
                  </div>
                  <div>
                    <dt className="text-sm uppercase tracking-wider text-fg-subtle">Longest continuous</dt>
                    <dd className="font-mono text-lg">{trace.metrics.longest_continuous_minutes.toFixed(0)} min</dd>
                    <dd className="text-[13px] text-fg-subtle">no separate limit in this protocol</dd>
                  </div>
                </dl>
              </Card>
            )}
            <Card title="What the agent found">
              <dl className="grid grid-cols-[140px_1fr] gap-y-2 text-sm">
                <dt className="text-fg-subtle">Observed</dt>
                <dd className="font-mono">{card.observed_value}</dd>
                <dt className="text-fg-subtle">Permitted</dt>
                <dd className="font-mono">{card.expected_value}</dd>
                <dt className="text-fg-subtle">Protocol</dt>
                <dd>{card.policy_clause}</dd>
              </dl>
              <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
                <div>
                  <div className="mb-1 text-sm uppercase tracking-wider text-fg-subtle">Verified</div>
                  <ul className="space-y-0.5">
                    {card.passed_checks.map((c) => (
                      <li key={c} className="text-pass-fg">
                        ✓ {CHECK_LABELS[c] ?? c}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="mb-1 text-sm uppercase tracking-wider text-fg-subtle">Blocked</div>
                  <ul className="space-y-0.5">
                    {card.blocked_checks.map((c) => (
                      <li key={c} className="text-fail-fg">
                        ✗ {CHECK_LABELS[c] ?? c}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
              <div className="mt-3 font-mono text-[12px] text-fg-subtle">evidence: {card.evidence_refs.join(" ")}</div>
            </Card>
            {result ? (
              <Card title="Outcome">
                <RunResultCard r={result} />
                <div className="mt-3">
                  <Link href={`/cases/${caseId}`} className="text-sm text-accent hover:underline">
                    ← Back to the case
                  </Link>
                </div>
              </Card>
            ) : (
              <Card title="Your decision">
                {refused && (
                  <div className="mb-3 rounded border border-warn-border bg-warn-bg px-3 py-2 text-sm text-warn-fg">
                    <div className="font-semibold">{refused.option.replaceAll("_", " ")} was refused for your role.</div>
                    <div className="mt-1 text-warn-fg">
                      The deterministic policy engine requires {card.options.find((o) => o.option === refused.option)?.required_roles.map((r) => r.replaceAll("_", " ").toLowerCase()).join(" or ")}. Nothing changed: {refused.result.logical_effect_count} domain effects, {refused.result.unauthorized_acceptances} unauthorized acceptances, {refused.result.stable_state.replaceAll("_", " ").toLowerCase()}.
                    </div>
                  </div>
                )}
                <textarea value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (recorded with the decision)" className="mb-3 w-full rounded border border-border bg-surface-2 p-2 text-sm" rows={2} />
                <div className="flex flex-col gap-3 sm:flex-row">
                  {card.options.map((o) => (
                    <div key={o.option} className="flex-1 rounded border border-border bg-surface-2 p-3">
                      <Button
                        variant={IRREVERSIBLE.has(o.option) ? "danger" : "primary"}
                        onClick={() => (IRREVERSIBLE.has(o.option) && confirming !== o.option ? setConfirming(o.option) : choose(o.option))}
                        disabled={busy !== null}
                      >
                        {busy === o.option
                          ? "Applying…"
                          : confirming === o.option
                            ? "Confirm; this cannot be undone"
                            : (OPTION_LABELS[o.option] ?? o.option.replaceAll("_", " "))}
                      </Button>
                      {confirming === o.option && (
                        <button onClick={() => setConfirming(null)} className="ml-2 text-sm text-fg-muted hover:text-fg">
                          cancel
                        </button>
                      )}
                      <p className="mt-2 text-sm text-fg-muted">{o.consequence}</p>
                      <p className="mt-1 text-[12px] text-fg-subtle">requires: {o.required_roles.map((r) => r.replaceAll("_", " ").toLowerCase()).join(", ")}</p>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-sm text-fg-subtle">Your role is assigned by the server from your sign-in, never from this page. An option your role cannot take will be refused.</p>
                <div className="mt-2">
                  <ErrorBox error={error} />
                </div>
              </Card>
            )}
          </>
        )}
        {card === undefined && <ErrorBox error={error} />}
      </div>
    </>
  );
}
