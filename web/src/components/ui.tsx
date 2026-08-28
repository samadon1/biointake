"use client";

import Link from "next/link";
import { useEffect, useState, useSyncExternalStore } from "react";
import { api, currentToken, signOut, subscribeSession, type RunResult, type Session } from "@/lib/api";

const STATE_STYLES: Record<string, string> = {
  CREATED: "bg-neutral-bg text-neutral-fg",
  VERIFYING: "bg-info-bg text-info-fg",
  WAITING_FOR_EVIDENCE: "bg-warn-bg text-warn-fg",
  NEEDS_HUMAN_DECISION: "bg-decision-bg text-decision-fg",
  COMPLETED: "bg-pass-bg text-pass-fg",
  FAILED: "bg-fail-bg text-fail-fg",
  PENDING: "bg-neutral-bg text-neutral-fg",
  ACCEPTED: "bg-pass-bg text-pass-fg",
  ACCEPTED_WITH_EXCEPTION: "bg-info-bg text-info-fg",
  QUARANTINED: "bg-fail-bg text-fail-fg",
  ERROR: "bg-fail-bg text-fail-fg",
  SATISFIED: "bg-pass-bg text-pass-fg",
  ACTIVE: "bg-warn-bg text-warn-fg",
};

export function StateBadge({ state, small = false }: { state: string; small?: boolean }) {
  const cls = STATE_STYLES[state] ?? "bg-neutral-bg text-neutral-fg";
  return (
    <span
      className={`inline-block rounded-md border border-current/20 font-mono font-medium uppercase tracking-wide ${
        small ? "px-1.5 py-px text-[12px]" : "px-1.5 py-0.5 text-[13px]"
      } ${cls}`}
    >
      {state.replaceAll("_", " ")}
    </span>
  );
}

export const CHECK_GLYPH_LABEL: Record<string, string> = {
  PASS: "passed",
  FAIL: "failed",
  UNAVAILABLE: "awaiting evidence",
  AMBIGUOUS: "ambiguous, needs confirmation",
  ERROR: "evaluator error",
};

export function CheckCell({
  status,
  provisional,
  label,
  onClick,
  active,
}: {
  status: string | null | undefined;
  provisional?: boolean;
  label?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  const map: Record<string, [string, string]> = {
    PASS: ["✓", "bg-pass-bg text-pass-fg border-pass-border"],
    FAIL: ["✗", "bg-fail-bg text-fail-fg border-fail-border"],
    UNAVAILABLE: ["…", "bg-warn-bg text-warn-fg border-warn-border"],
    AMBIGUOUS: ["?", "bg-warn-bg text-warn-fg border-warn-border border-dashed"],
    ERROR: ["!", "bg-fail-bg text-fail-fg border-fail-border"],
  };
  const [glyph, cls] = status && map[status] ? map[status] : ["·", "bg-surface-2 text-fg-subtle border-border"];
  const described = `${label ?? "check"}: ${status ? (CHECK_GLYPH_LABEL[status] ?? status) : "not evaluated"}${provisional ? ", provisional" : ""}`;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      aria-label={described}
      title={described}
      className={`inline-flex h-7 w-9 items-center justify-center rounded-md border font-mono text-sm transition ${cls} ${onClick ? "cursor-pointer hover:brightness-95 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none dark:hover:brightness-125" : ""} ${active ? "ring-2 ring-accent" : ""}`}
    >
      <span aria-hidden>{glyph}</span>
      {provisional && (
        <span aria-hidden className="ml-0.5 text-[12px] text-amber-400">
          ~
        </span>
      )}
    </button>
  );
}

/** Age is the primary sort key for outstanding work: research on query management shows a median
 *  resolution of ~23 days with a long tail, so elapsed time belongs in the list, not on a detail page. */
export const DISCREPANCY_SLA_HOURS = 24; // GTEx OP-0011 §8.10.2: "any discrepancy resolution request should
// be addressed within 24 hours of receipt", the one hard number anyone publishes for this.

export const AGE_BUCKETS: { maxDays: number; label: string; cls: string }[] = [
  { maxDays: 1, label: "within SLA (24h)", cls: "text-fg-muted" },
  { maxDays: 5, label: "1–5 days", cls: "text-warn-fg" },
  { maxDays: 14, label: "6–14 days", cls: "text-fg" },
  { maxDays: 30, label: "15–30 days", cls: "text-warn-fg" },
  { maxDays: 90, label: "30+ days", cls: "text-warn-fg font-semibold" },
  { maxDays: Infinity, label: "90+ days, escalate", cls: "text-fail-fg font-semibold" },
];

export function ageOf(iso: string): { ms: number; days: number; bucket: (typeof AGE_BUCKETS)[number] } {
  const ms = Math.max(0, Date.now() - new Date(iso).getTime());
  const days = ms / 86_400_000;
  return { ms, days, bucket: AGE_BUCKETS.find((b) => days <= b.maxDays) ?? AGE_BUCKETS[AGE_BUCKETS.length - 1] };
}

export function humanAge(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)} days`;
}

/** Live-updating elapsed time. Ticks once a second so the demo shows a moving clock. */
export function Elapsed({ since, prefix = "", suffix = " ago", warnAfterMs }: { since: string; prefix?: string; suffix?: string; warnAfterMs?: number }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const { ms } = ageOf(since);
  const warn = warnAfterMs !== undefined && ms > warnAfterMs;
  return (
    <time dateTime={since} title={new Date(since).toLocaleString()} className={`tabular-nums ${warn ? "text-warn-fg" : ""}`}>
      {prefix}
      {humanAge(ms)}
      {suffix}
    </time>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-surface-2 ${className}`} />;
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <div className="rounded border border-dashed border-border px-4 py-8 text-center">
      <p className="text-sm text-fg-muted">{title}</p>
      {hint && <p className="mt-1 text-sm text-fg-subtle">{hint}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Card({ title, children, right, className = "" }: { title?: string; children: React.ReactNode; right?: React.ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <h2 className="eyebrow">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Button({ children, onClick, disabled, variant = "primary", type = "button" }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean; variant?: "primary" | "ghost" | "danger"; type?: "button" | "submit" }) {
  const cls = {
    primary: "bg-accent text-accent-fg hover:brightness-110",
    ghost: "border border-border bg-surface text-fg hover:bg-surface-2 hover:border-border-strong",
    danger: "bg-fail-fg text-fail-bg hover:brightness-110",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled} className={`rounded-md px-3.5 py-2 text-sm font-semibold transition focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${cls}`}>
      {children}
    </button>
  );
}

/** Who the server says you are, and the way out. The name here is not a choice the browser makes:
 *  it is whatever the token resolves to, which is the same thing the audit trail will record. */
export function SessionBadge() {
  const token = useSyncExternalStore(subscribeSession, currentToken, () => "");
  const [session, setSession] = useState<Session | null>(null);
  useEffect(() => {
    if (!token) return;
    let live = true;
    api
      .me()
      .then((s) => {
        if (live) setSession(s);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [token]);
  // A signed-out badge shows nothing regardless of what the last fetch left behind.
  if (!token || !session) return null;
  return (
    <div className="flex items-center gap-3 text-sm text-fg-muted">
      <span>
        <span className="text-fg">{session.display_name}</span>
      </span>
      <button onClick={signOut} className="rounded border border-border px-2 py-1 text-sm hover:bg-surface">
        Sign out
      </button>
    </div>
  );
}

/** A failure that owns the page. An error stripe pinned to the top of an otherwise blank screen tells
 *  someone that something broke and gives them nowhere to go; this says what happened, states plainly that
 *  nothing was changed, and offers the way back. */
export function PageError({
  error,
  onRetry,
  backHref,
  backLabel,
}: {
  error: string;
  onRetry?: () => void;
  backHref: string;
  backLabel: string;
}) {
  const missing = /not found/i.test(error);
  return (
    <div className="mx-auto w-full max-w-xl p-8">
      <div className="panel p-6 text-center">
        <h2 className="text-lg font-semibold text-fg">
          {missing ? "That shipment is not here." : "This page could not load."}
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-fg-muted">
          {missing
            ? "It may have been reset, or the link may be out of date. Nothing has been changed."
            : "The control API did not answer. Nothing has been changed."}
        </p>
        <p className="mt-3 font-mono text-[13px] text-fg-subtle">{error.replace(/^ApiError:\s*/, "")}</p>
        <div className="mt-5 flex items-center justify-center gap-2">
          <Link
            href={backHref}
            className="rounded-md bg-accent px-3.5 py-2 text-sm font-semibold text-accent-fg hover:brightness-110"
          >
            {backLabel}
          </Link>
          {onRetry && !missing && (
            <button
              onClick={onRetry}
              className="rounded-md border border-border bg-surface px-3.5 py-2 text-sm font-semibold text-fg hover:bg-surface-2"
            >
              Try again
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null;
  const friendly = error.includes("Failed to fetch")
    ? "Cannot reach the control API. Is it running on :8000? (make api-dev)"
    : error.replace(/^ApiError:\s*/, "");
  return (
    <div role="alert" className="flex items-start justify-between gap-3 rounded border border-fail-border bg-fail-bg px-3 py-2 text-sm text-fail-fg">
      <span>{friendly}</span>
      {onRetry && (
        <button onClick={onRetry} className="shrink-0 rounded border border-fail-border px-2 py-0.5 text-sm hover:brightness-95">
          Retry
        </button>
      )}
    </div>
  );
}

export function AgentPulse({ running }: { running: boolean }) {
  if (!running) return null;
  return (
    <span className="inline-flex items-center gap-2 rounded bg-info-bg px-2 py-1 text-sm text-info-fg" role="status" aria-live="polite">
      <span className="h-1.5 w-1.5 animate-ping rounded-full bg-info-fg" />
      agent working…
    </span>
  );
}

export function RunResultCard({ r }: { r: RunResult }) {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-1 rounded border border-border bg-surface-2 p-3 font-mono text-sm text-fg-muted md:grid-cols-4">
      <div>
        stable state <StateBadge state={r.stable_state} small />
      </div>
      <div>stop reason: {r.stop_reason}</div>
      <div>checks evaluated: {r.checks_evaluated}</div>
      <div>re-verified: {r.checks_reverified}</div>
      <div>tool attempts: {r.tool_attempt_count}</div>
      <div>domain effects: {r.logical_effect_count}</div>
      <div>denials: {r.intervention_denials}</div>
      <div>unauthorized: {r.unauthorized_acceptances}</div>
      {r.boot_id && <div className="col-span-2">runtime boot: {r.boot_id.slice(0, 8)}…</div>}
      {r.warnings.length > 0 && <div className="col-span-4 text-warn-fg">{r.warnings.join(" · ")}</div>}
    </div>
  );
}

