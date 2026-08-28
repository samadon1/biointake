"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, pollInterval, type CaseSummary } from "@/lib/api";
import { Button, Card, Elapsed, EmptyState, ErrorBox, Skeleton, StateBadge } from "@/components/ui";
import { PageHeader } from "@/components/shell";

/** States that live before verification: the box is expected, or open on the bench. */
const PRE_ARRIVAL = new Set(["ANNOUNCED", "RECEIVED"]);

export default function QueuePage() {
  const router = useRouter();
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
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
    const t = setInterval(refresh, pollInterval(busy !== null));
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [refresh, busy]);

  async function loadDemo() {
    setBusy("Loading the example shipment…");
    setError(null);
    try {
      await api.demoReset();
      const loaded = await api.demoLoad();
      // Navigate first: the case workspace starts the agent and streams its work as it happens.
      router.push(`/cases/${loaded.case_id}?run=1`);
    } catch (e) {
      setError(String(e));
      setBusy(null);
    }
  }

  return (
    <>
      <PageHeader
        title="Shipment queue"
        meta="Everything in reconciliation, and what each one needs next"
        actions={
          <>
            {/* Announcing is the work; the example is a shortcut past the data entry for someone
                who wants to see where a case ends up. The weight goes to the work. */}
            <Button variant="ghost" onClick={loadDemo} disabled={busy !== null}>
              {busy ?? "Load the example shipment"}
            </Button>
            <Link href="/announce" className="rounded-md bg-accent px-3.5 py-2 text-sm font-semibold text-accent-fg transition hover:brightness-110">
              Announce a shipment
            </Link>
          </>
        }
      />
      <div className="w-full space-y-4 p-4 sm:p-5">
        <ErrorBox error={error} onRetry={refresh} />
        <Card
          title="Open shipments"
          right={
            cases && cases.length > 0 ? (
              <span className="text-[13px] text-fg-subtle tabular-nums">{cases.length}</span>
            ) : undefined
          }
        >
          {cases === null ? (
            <div className="space-y-2">
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
            </div>
          ) : cases.length === 0 ? (
            <EmptyState
              title="No shipments yet."
              hint="Announce a shipment to start one, or load the example, twelve specimens with a manifest typo, a cold-chain excursion, two participants whose consent is out of date, and an accession that belongs to an archived record. Both take the same path through the product."
            />
          ) : (
            <div className="-mx-4 overflow-x-auto px-4">
              {/* Deliberately five columns, not eight. State, "needs you" and "next action" were three
                  columns restating one fact; a queue is read by scanning down a single decisive column, and
                  that column is what to do next. */}
              <table className="w-full min-w-[46rem] text-sm">
                <thead className="text-left text-fg-subtle">
                  <tr className="border-b border-border">
                    <th scope="col" className="eyebrow pb-2 pl-3 font-semibold">Shipment</th>
                    <th scope="col" className="eyebrow pb-2 font-semibold">State</th>
                    <th scope="col" className="eyebrow pb-2 text-right font-semibold">Specimens</th>
                    <th scope="col" className="eyebrow pb-2 pl-6 font-semibold">Blocked on</th>
                    <th scope="col" className="eyebrow pb-2 font-semibold">Next action</th>
                    <th scope="col" className="eyebrow pb-2 text-right font-semibold">Last activity</th>
                  </tr>
                </thead>
                <tbody>
                  {cases.map((c) => {
                    const inbound = PRE_ARRIVAL.has(c.state);
                    const href = inbound ? `/receive/${c.case_id}` : `/cases/${c.case_id}`;
                    const rule = c.pending_decisions
                      ? "border-l-decision-fg"
                      : c.active_requests
                        ? "border-l-warn-fg"
                        : inbound
                          ? "border-l-accent"
                          : "border-l-transparent";
                    return (
                      <tr key={c.case_id} className={`border-b border-border/60 border-l-2 last:border-b-0 hover:bg-surface-2 ${rule}`}>
                        <th scope="row" className="py-2.5 pl-3 text-left font-normal">
                          <Link href={href} className="block">
                            <span className="font-mono font-semibold text-fg">{c.shipment_id}</span>
                            <span className="mt-0.5 block font-mono text-[12px] text-fg-subtle">{c.case_id}</span>
                          </Link>
                        </th>
                        <td className="py-2.5"><StateBadge state={c.state} /></td>
                        <td className="py-2.5 text-right tabular-nums">
                          {inbound ? (
                            <span className="text-fg-muted">
                              {c.declared ?? "-"} <span className="text-[12px] text-fg-subtle">declared</span>
                            </span>
                          ) : (
                            c.samples
                          )}
                        </td>
                        <td className="py-2.5 pl-6">
                          {c.pending_decisions ? (
                            <span className="text-decision-fg">You · {c.pending_decisions} decision</span>
                          ) : c.active_requests ? (
                            <span className="text-warn-fg">The site · {c.active_requests} request</span>
                          ) : (
                            <span className="text-fg-subtle">-</span>
                          )}
                        </td>
                        <td className="py-2.5">
                          {inbound ? (
                            <Link href={href} className="font-medium text-accent hover:underline">
                              {c.state === "ANNOUNCED" ? "Record receipt" : "Scan tubes"} &rarr;
                            </Link>
                          ) : c.pending_decisions ? (
                            // The one row that cannot move without a person gets the only button-shaped
                            // affordance in the table. Everything else is a link, because everything else
                            // is somewhere to look rather than something to do.
                            <Link
                              href={href}
                              className="inline-block rounded-md border border-decision-border bg-decision-bg px-2.5 py-1 font-semibold text-decision-fg transition hover:brightness-110"
                            >
                              Decide &rarr;
                            </Link>
                          ) : c.active_requests ? (
                            <span className="text-fg-muted">Waiting on the site</span>
                          ) : (
                            <Link href={href} className="text-fg-muted hover:text-fg hover:underline">
                              Review
                            </Link>
                          )}
                        </td>
                        <td className="py-2.5 text-right text-fg-subtle">
                          <Elapsed since={c.updated_at} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <section className="panel p-4">
          <h2 className="text-sm font-semibold tracking-tight text-fg">What this does</h2>
          <p className="mt-1.5 max-w-4xl text-sm leading-relaxed text-fg-muted">
            Before a tube enters the freezer, seven records have to agree: the label, the shipping manifest,
            the study protocol, the participant&apos;s consent, the transport temperature log, the chain of
            custody, and the lab&apos;s own record system. A coordinator reconciles that by hand today, then
            chases whatever is missing by email.
          </p>
          <p className="mt-1.5 max-w-4xl text-sm leading-relaxed text-fg-muted">
            BioIntake does the reconciliation and the chasing. A specimen is accepted only when every
            required check passes, and that decision is made by a deterministic policy engine, never by the
            model. A person is interrupted only where the protocol reserves the judgement for one.
          </p>
          <p className="mt-2.5 text-[13px] text-fg-subtle">
            All data is synthetic. A research-operations demonstration, not clinical or regulatory software.
          </p>
        </section>
      </div>
    </>
  );
}
