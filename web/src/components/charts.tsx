"use client";

/** Small dependency-free SVG charts. Every chart states its numbers in text too, so the
 *  information is never colour- or vision-dependent. */

export function DispositionBar({
  counts,
  showLegend = true,
}: {
  counts: Record<string, number>;
  showLegend?: boolean;
}) {
  const order: { key: string; label: string; cls: string }[] = [
    { key: "ACCEPTED", label: "Accepted", cls: "bg-pass-solid" },
    { key: "ACCEPTED_WITH_EXCEPTION", label: "Accepted with exception", cls: "bg-info-solid" },
    { key: "WAITING_FOR_EVIDENCE", label: "Waiting for evidence", cls: "bg-warn-solid" },
    { key: "NEEDS_HUMAN_DECISION", label: "Needs a decision", cls: "bg-decision-solid" },
    { key: "QUARANTINED", label: "On hold", cls: "bg-fail-solid" },
    { key: "REJECTED", label: "Rejected", cls: "bg-fail-solid" },
    { key: "PENDING", label: "Not yet verified", cls: "bg-neutral-solid" },
    { key: "ERROR", label: "Error", cls: "bg-fail-solid" },
  ];
  const total = order.reduce((n, o) => n + (counts[o.key] ?? 0), 0) || 1;
  const present = order.filter((o) => (counts[o.key] ?? 0) > 0);
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface-2" role="img" aria-label={present.map((o) => `${counts[o.key]} ${o.label}`).join(", ")}>
        {present.map((o) => (
          <div key={o.key} className={o.cls} style={{ width: `${((counts[o.key] ?? 0) / total) * 100}%` }} title={`${counts[o.key]} ${o.label}`} />
        ))}
      </div>
      {showLegend && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-fg-muted">
          {present.map((o) => (
            <li key={o.key} className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${o.cls}`} aria-hidden />
              {counts[o.key]} {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export type TempPoint = { t: number; c: number };

/** Transport temperature trace with the permitted band shaded; the excursion is the story. */
export function TemperatureTrace({
  points,
  min,
  max,
  height = 120,
  label,
  caption = true,
}: {
  points: TempPoint[];
  min: number;
  max: number;
  height?: number;
  label?: string;
  caption?: boolean;
}) {
  if (points.length < 2) return null;
  const w = 640;
  const pad = { l: 28, r: 8, t: 8, b: 16 };
  const xs = points.map((p) => p.t);
  const ys = points.map((p) => p.c);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(min - 1, ...ys);
  const y1 = Math.max(max + 1, ...ys);
  const sx = (t: number) => pad.l + ((t - x0) / (x1 - x0 || 1)) * (w - pad.l - pad.r);
  const sy = (c: number) => pad.t + (1 - (c - y0) / (y1 - y0 || 1)) * (height - pad.t - pad.b);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.t).toFixed(1)},${sy(p.c).toFixed(1)}`).join(" ");
  const peak = points.reduce((a, b) => (b.c > a.c ? b : a), points[0]);
  return (
    <figure>
      <svg viewBox={`0 0 ${w} ${height}`} className="w-full" role="img" aria-label={label ?? `Transport temperature: peak ${peak.c.toFixed(1)} °C against a permitted band of ${min} to ${max} °C`}>
        <rect x={pad.l} y={sy(max)} width={w - pad.l - pad.r} height={Math.max(1, sy(min) - sy(max))} className="fill-pass-bg" />
        <line x1={pad.l} y1={sy(max)} x2={w - pad.r} y2={sy(max)} className="stroke-pass-border" strokeDasharray="3 3" />
        <line x1={pad.l} y1={sy(min)} x2={w - pad.r} y2={sy(min)} className="stroke-pass-border" strokeDasharray="3 3" />
        <path d={d} fill="none" className="stroke-fg" strokeWidth="1.5" />
        <circle cx={sx(peak.t)} cy={sy(peak.c)} r="3" className="fill-fail-fg" />
        <text x={pad.l - 4} y={sy(max) + 4} textAnchor="end" className="fill-fg-subtle text-[12px]">{max}°</text>
        <text x={pad.l - 4} y={sy(min) + 4} textAnchor="end" className="fill-fg-subtle text-[12px]">{min}°</text>
      </svg>
      {/* The caption is off where the surrounding card already names the permitted band and the peak. Two
          statements of one fact is not emphasis, it is noise. */}
      {caption && (
        <figcaption className="mt-1 text-sm text-fg-muted">
          Peak {peak.c.toFixed(1)} °C · permitted {min}–{max} °C
        </figcaption>
      )}
    </figure>
  );
}

/** Verification progress as a compact stacked count, used while the agent is working. */
export function CheckProgress({ done, total }: { done: number; total: number }) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return (
    // Stacks in a narrow sidebar and sits inline where there is room, rather than wrapping mid-phrase.
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted">
      <div className="h-1.5 min-w-24 grow overflow-hidden rounded-full bg-surface-2">
        {/* Steel, not the action accent. This is a completeness readout, and it was the loudest thing
            in a sidebar whose most important element is the disposition bar above it. */}
        <div className="h-full bg-info-solid transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
      <span aria-live="polite" className="whitespace-nowrap tabular-nums">
        {done}/{total} checks
      </span>
    </div>
  );
}
