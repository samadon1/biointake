"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, pollInterval, type CaseSummary } from "@/lib/api";
import { Card, Elapsed, EmptyState, ErrorBox, Skeleton, StateBadge } from "@/components/ui";
import { PageHeader } from "@/components/shell";

/** Shipments that have not yet reached verification: expected, or open on the bench. */
const PRE_ARRIVAL = new Set(["ANNOUNCED", "RECEIVED"]);

export function ReceivingQueue() {
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCases(await api.listCases());
      setError(null);
    } catch (e) {
      setError(String(e));
      setCases([]);
    }
  }, []);

  useEffect(() => {
    const first = setTimeout(refresh, 0);
    const t = setInterval(refresh, pollInterval(false));
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [refresh]);

  const inbound = (cases ?? []).filter((c) => PRE_ARRIVAL.has(c.state));

  return (
    <>
      <PageHeader
        title="Receiving"
        meta="Shipments a site has announced, and boxes open on the bench"
        actions={
          <Link href="/announce" className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium text-fg hover:bg-surface-2">
            Announce a shipment
          </Link>
        }
      />
      <div className="mx-auto w-full max-w-5xl p-4 sm:p-6">
        <ErrorBox error={error} onRetry={refresh} />
        {cases === null ? (
          <Skeleton className="h-32 w-full" />
        ) : inbound.length === 0 ? (
          <EmptyState
            title="Nothing inbound."
            hint="A shipment appears here the moment a sending site announces it, before the box arrives, so the bench knows what to expect."
          />
        ) : (
          <div className="space-y-3">
            {inbound.map((c) => (
              <Link
                key={c.case_id}
                href={`/receive/${c.case_id}`}
                className="block rounded-lg border-l-4 border-l-accent border-y border-r border-border bg-surface p-4 shadow-[var(--shadow)] transition hover:bg-surface-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-base font-semibold text-fg">{c.shipment_id}</span>
                      <StateBadge state={c.state} />
                    </div>
                    <p className="mt-0.5 text-sm text-fg-muted">
                      {c.state === "ANNOUNCED" ? "Expected, record receipt when the box arrives" : "On the bench, scan the tubes"}
                    </p>
                  </div>
                  <div className="text-right">
                    <div className="text-sm font-medium text-accent">
                      {c.state === "ANNOUNCED" ? "Record receipt" : "Scan tubes"} →
                    </div>
                    <div className="mt-0.5 text-[13px] text-fg-subtle">
                      <Elapsed since={c.updated_at} />
                    </div>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
        <Card className="mt-6">
          <p className="text-sm text-fg-muted">
            Verification happens after the batch is committed. Shipments already being reconciled are in the{" "}
            <Link href="/" className="text-accent hover:underline">
              queue
            </Link>
            .
          </p>
        </Card>
      </div>
    </>
  );
}
