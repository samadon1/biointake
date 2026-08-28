"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type ManifestValidation, type Study } from "@/lib/api";
import { Button, Card, ErrorBox, Skeleton } from "@/components/ui";

/* ------------------------------------------------------------------------------------------------
   Advance notification, the sending site's side of the front door.

   This exists because of one finding that repeats across every source: the expensive failures are the
   ones caught after the box has already been packed and shipped. A manifest that names a specimen type
   the study does not collect costs an email to fix here, and a cold-chain excursion plus a destroyed
   specimen to fix on arrival. So the manifest is checked against the study *before* the courier is
   booked, and the site is told exactly which rows are wrong.

   No account, no install: a site coordinator follows a link and fills this in.
------------------------------------------------------------------------------------------------ */

function Warnings({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  return (
    <div className="mt-2 rounded border border-warn-border bg-warn-bg px-3 py-2 text-sm text-warn-fg">
      <ul className="list-disc space-y-0.5 pl-5">
        {warnings.map((w, i) => (
          <li key={i}>{w}</li>
        ))}
      </ul>
    </div>
  );
}

function ProblemList({ v }: { v: ManifestValidation }) {
  if (v.accepted) {
    return (
      <div>
        <div className="rounded border border-pass-border bg-pass-bg px-3 py-2 text-sm text-pass-fg">
          <strong>{v.lines.length} specimens</strong> read cleanly. {v.summary}
        </div>
        <Warnings warnings={v.warnings} />
      </div>
    );
  }
  return (
    <div>
      <div className="rounded border border-fail-border bg-fail-bg px-3 py-2 text-sm text-fail-fg">
      <p className="font-medium">This manifest cannot be accepted yet.</p>
      <ul className="mt-1.5 list-disc space-y-0.5 pl-5">
        {v.problems.map((p, i) => (
          <li key={i}>{p}</li>
        ))}
      </ul>
      <p className="mt-2 text-sm opacity-80">Fix these rows and re-check. Nothing is sent to the lab until it reads cleanly.</p>
      </div>
      <Warnings warnings={v.warnings} />
    </div>
  );
}

export function AnnounceForm() {
  const [studies, setStudies] = useState<Study[] | null>(null);
  const [studyId, setStudyId] = useState("");
  const [shipmentId, setShipmentId] = useState("");
  const [siteId, setSiteId] = useState("SITE-NORTHSIDE");
  const [contactId, setContactId] = useState("SITE-CONTACT-002");
  const [courier, setCourier] = useState("");
  const [tracking, setTracking] = useState("");
  const [containers, setContainers] = useState(1);
  const [loggerIds, setLoggerIds] = useState("");
  const [shippingCondition, setShippingCondition] = useState("dry ice");
  const [expectedArrival, setExpectedArrival] = useState("");
  const [csv, setCsv] = useState("");
  const [filename, setFilename] = useState("");
  // The paperwork the site holds and the lab does not. Optional here on purpose: a box that turns
  // up without it is the ordinary case, and BioIntake's answer to that is to ask for it rather than
  // to refuse the announcement.
  const [custody, setCustody] = useState<{ name: string; b64: string } | null>(null);
  const [consent, setConsent] = useState<{ name: string; b64: string } | null>(null);
  const [validation, setValidation] = useState<ManifestValidation | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ case_id: string; declared: number } | null>(null);

  useEffect(() => {
    api
      .studies()
      .then((s) => {
        setStudies(s);
        if (s.length) setStudyId(s[0].study_id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  function b64(text: string): string {
    return btoa(unescape(encodeURIComponent(text)));
  }

  // Checked as it is typed or pasted, not on blur: a coordinator pasting a manifest should learn it is
  // wrong without having to click somewhere else to find out.
  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      if (!csv.trim() || !studyId) {
        if (!cancelled) {
          setValidation(null);
          setChecking(false);
        }
        return;
      }
      if (!cancelled) setChecking(true);
      try {
        const v = await api.validateManifest(studyId, b64(csv));
        if (!cancelled) {
          setValidation(v);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setChecking(false);
      }
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [csv, studyId]);

  async function onFile(file: File | null) {
    if (!file) return;
    setFilename(file.name);
    setCsv(await file.text());
  }

  async function onDocument(file: File | null, set: (v: { name: string; b64: string } | null) => void) {
    if (!file) {
      set(null);
      return;
    }
    set({ name: file.name, b64: b64(await file.text()) });
  }

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.announce({
        shipment_id: shipmentId.trim(),
        study_id: studyId,
        sender_site_id: siteId.trim(),
        announced_by_contact_id: contactId.trim(),
        manifest_csv_base64: b64(csv),
        courier,
        tracking_reference: tracking,
        expected_arrival: expectedArrival ? new Date(expectedArrival).toISOString() : null,
        container_count: containers,
        logger_ids: loggerIds.split(",").map((s) => s.trim()).filter(Boolean),
        shipping_condition: shippingCondition,
        custody_log_base64: custody?.b64 ?? null,
        consent_records_base64: consent?.b64 ?? null,
      });
      setDone({ case_id: r.case_id, declared: r.declared_specimens });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (studies === null) return <Skeleton className="h-64 w-full" />;

  if (done) {
    return (
      <Card title="Announced">
        <p className="text-sm text-fg">
          The lab is expecting <strong>{done.declared} specimens</strong> under {shipmentId}. Ship when ready.
        </p>
        <p className="mt-2 text-sm text-fg-muted">
          When the box arrives the lab receives against exactly this manifest, so anything that differs,
          a tube that is not on the list, an identifier that reads differently, comes back to you as a
          specific question rather than a vague query weeks later.
        </p>
        <div className="mt-4 flex gap-2">
          <Link href={`/receive/${done.case_id}`} className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg hover:brightness-110">
            Open the receiving bench
          </Link>
          <Link href="/" className="rounded border border-border px-3 py-1.5 text-sm text-fg hover:bg-surface-2">
            Back to the queue
          </Link>
        </div>
      </Card>
    );
  }

  const ready = Boolean(validation?.accepted && shipmentId.trim() && siteId.trim() && contactId.trim());

  return (
    <div className="space-y-4">
      <Card title="Manifest">
        <p className="mb-3 text-sm text-fg-muted">
          A CSV with one row per specimen. It is checked against the study now, before you book the courier.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="text-sm text-fg-muted">
            Study
            <select
              value={studyId}
              onChange={(e) => setStudyId(e.target.value)}
              className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
            >
              {studies.map((s) => (
                <option key={s.study_id} value={s.study_id}>
                  {s.name} ({s.study_id})
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm text-fg-muted">
            Manifest file
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
              className="mt-1 block w-full text-sm text-fg-muted file:mr-3 file:rounded file:border-0 file:bg-accent file:px-3 file:py-1.5 file:text-sm file:text-accent-fg"
            />
            {filename && <span className="mt-1 block font-mono text-[13px] text-fg-subtle">{filename}</span>}
          </label>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="text-sm text-fg-muted">
            Chain-of-custody log <span className="text-fg-subtle">(optional)</span>
            <input
              type="file"
              accept=".json,application/json"
              onChange={(e) => void onDocument(e.target.files?.[0] ?? null, setCustody)}
              className="mt-1 block w-full text-sm text-fg-muted file:mr-3 file:rounded file:border-0 file:bg-surface-2 file:px-3 file:py-1.5 file:text-sm file:text-fg"
            />
            {custody && <span className="mt-1 block font-mono text-[13px] text-fg-subtle">{custody.name}</span>}
          </label>
          <label className="text-sm text-fg-muted">
            Consent registry <span className="text-fg-subtle">(optional)</span>
            <input
              type="file"
              accept=".json,application/json"
              onChange={(e) => void onDocument(e.target.files?.[0] ?? null, setConsent)}
              className="mt-1 block w-full text-sm text-fg-muted file:mr-3 file:rounded file:border-0 file:bg-surface-2 file:px-3 file:py-1.5 file:text-sm file:text-fg"
            />
            {consent && <span className="mt-1 block font-mono text-[13px] text-fg-subtle">{consent.name}</span>}
          </label>
        </div>
        <p className="mt-2 text-[13px] text-fg-subtle">
          Send these now if you have them. Without them the lab can still receive the box, and will
          write to ask you for them once it has.
        </p>
        <label className="mt-4 block text-sm text-fg-muted">
          …or paste it
          <textarea
            value={csv}
            onChange={(e) => setCsv(e.target.value)}
            rows={6}
            spellCheck={false}
            placeholder="sample_id,participant_reference,specimen_type,container_id,collection_timestamp"
            className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 font-mono text-sm text-fg"
          />
        </label>
        <div className="mt-3">
          {checking ? <Skeleton className="h-9 w-full" /> : validation ? <ProblemList v={validation} /> : null}
        </div>
      </Card>

      <Card title="Shipment">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="text-sm text-fg-muted">
            Shipment reference
            <input value={shipmentId} onChange={(e) => setShipmentId(e.target.value)} placeholder="SHIP-2026-0043" className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Sending site
            <input value={siteId} onChange={(e) => setSiteId(e.target.value)} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Your contact ID
            <input value={contactId} onChange={(e) => setContactId(e.target.value)} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
            <span className="mt-1 block text-[13px] text-fg-subtle">Must already be verified with the lab.</span>
          </label>
          <label className="text-sm text-fg-muted">
            Courier
            <input value={courier} onChange={(e) => setCourier(e.target.value)} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Tracking reference
            <input value={tracking} onChange={(e) => setTracking(e.target.value)} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Expected arrival
            <input type="datetime-local" value={expectedArrival} onChange={(e) => setExpectedArrival(e.target.value)} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Containers
            <input type="number" min={1} value={containers} onChange={(e) => setContainers(Number(e.target.value))} className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Shipping condition
            <input value={shippingCondition} onChange={(e) => setShippingCondition(e.target.value)} placeholder="dry ice" className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
          </label>
          <label className="text-sm text-fg-muted">
            Logger IDs
            <input value={loggerIds} onChange={(e) => setLoggerIds(e.target.value)} placeholder="LOG-1, LOG-2" className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg" />
            <span className="mt-1 block text-[13px] text-fg-subtle">Comma separated, one per container.</span>
          </label>
        </div>
      </Card>

      <ErrorBox error={error} />
      <div className="flex items-center justify-end gap-3">
        {!validation?.accepted && <span className="text-sm text-fg-subtle">The manifest has to read cleanly before you can announce.</span>}
        <Button onClick={submit} disabled={!ready || busy}>
          {busy ? "Announcing…" : "Announce shipment"}
        </Button>
      </div>
    </div>
  );
}
