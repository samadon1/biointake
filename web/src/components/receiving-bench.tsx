"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type BatchSummary,
  type ExpectedRow,
  type IntakeView,
  type ScanOutcome,
} from "@/lib/api";
import { Button, Card, ErrorBox, PageError, Skeleton, StateBadge } from "@/components/ui";
import { BarcodeScanner } from "@/components/barcode-scanner";
import { PageHeader } from "@/components/shell";
import { DiffedValue, glyphDifference } from "@/components/identifiers";

/* ------------------------------------------------------------------------------------------------
   The receiving bench.

   Two ideas from the research drive this screen:

   1. The manifest defines the rows and the scanner fills a single column (the Nautilus receiving
      pattern). Discrepancy detection then falls out of the interaction itself, a tech never has to
      compare two lists by eye, which is where errors are actually made.
   2. Nothing reaches inventory until the batch is explicitly committed (BSI's staging pattern). Up to
      that moment the bench is a scratchpad; after it, the samples exist and the agent can reconcile.

   The tech is standing at a bench, often gloved, with a keyboard-wedge scanner. So: one focused input
   that never loses focus, immediate and unambiguous per-scan feedback, and nothing that requires the
   mouse on the happy path.
------------------------------------------------------------------------------------------------ */

const CONDITIONS: { value: string; label: string; hint: string }[] = [
  { value: "ACCEPTABLE", label: "Acceptable", hint: "Intact, nothing to note" },
  { value: "DAMAGED_USABLE", label: "Damaged, contents usable", hint: "Outer packaging compromised; tubes intact" },
  { value: "DAMAGED_NOT_USABLE", label: "Damaged, contents compromised", hint: "Leakage, breakage, or thaw evident" },
];

/* Received quality, per specimen. The vocabulary is OpenSpecimen's, because it is what biobank staff
   already read and write, and it is deliberately separate from the condition of the package: a box can
   arrive intact with one thawed tube inside it. */
const QUALITIES: { value: string; label: string }[] = [
  { value: "ACCEPTABLE", label: "Acceptable" },
  { value: "THAWED", label: "Thawed" },
  { value: "QUANTITY_NOT_SUFFICIENT", label: "Quantity not sufficient" },
  { value: "CLOTTED", label: "Clotted" },
  { value: "HEMOLYZED", label: "Hemolyzed" },
  { value: "LIPEMIC", label: "Lipemic" },
  { value: "DAMAGED", label: "Damaged" },
  { value: "UNACCEPTABLE", label: "Unacceptable" },
];

const OUTCOME_STYLE: Record<string, { row: string; chip: string; label: string }> = {
  MATCHED: { row: "bg-pass-bg", chip: "bg-pass-bg text-pass-fg border-pass-border", label: "Matched" },
  NEAR_MATCH: { row: "bg-warn-bg", chip: "bg-warn-bg text-warn-fg border-warn-border", label: "Near match" },
  UNEXPECTED: { row: "bg-fail-bg", chip: "bg-fail-bg text-fail-fg border-fail-border", label: "Not on manifest" },
  DUPLICATE: { row: "bg-fail-bg", chip: "bg-fail-bg text-fail-fg border-fail-border", label: "Duplicate" },
};


function Stat({ n, label, tone = "neutral" }: { n: number; label: string; tone?: "neutral" | "pass" | "warn" | "fail" }) {
  const cls = {
    neutral: "text-fg",
    pass: "text-pass-fg",
    warn: "text-warn-fg",
    fail: "text-fail-fg",
  }[tone];
  return (
    <div className="min-w-[4.5rem]">
      <div className={`text-2xl font-semibold tabular-nums ${n === 0 && tone !== "neutral" ? "text-fg-subtle" : cls}`}>{n}</div>
      <div className="text-[13px] uppercase tracking-wide text-fg-muted">{label}</div>
    </div>
  );
}

/* ---- stage 1: what the box looked like when it arrived --------------------------------------- */

function ReceiptForm({ view, onDone }: { view: IntakeView; onDone: () => void }) {
  const declared = view.announcement?.container_count ?? 1;
  const [condition, setCondition] = useState("ACCEPTABLE");
  const [count, setCount] = useState(declared);
  const [refrigerant, setRefrigerant] = useState("");
  const [tempC, setTempC] = useState("");
  const [seal, setSeal] = useState(true);
  const [notes, setNotes] = useState("");
  const [files, setFiles] = useState<{ filename: string; content_base64: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const receiptRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  async function readFiles(list: FileList | null) {
    if (!list) return;
    const out: { filename: string; mime_type: string; content_base64: string }[] = [];
    for (const f of Array.from(list)) {
      const buf = await f.arrayBuffer();
      let bin = "";
      new Uint8Array(buf).forEach((b) => (bin += String.fromCharCode(b)));
      // Some browsers report no type for .csv. The API's allowlist is what decides whether the file
      // is admissible, so guessing text/csv here only gets it as far as that check.
      out.push({ filename: f.name, mime_type: f.type || "text/csv", content_base64: btoa(bin) });
    }
    setFiles((prev) => [...prev, ...out]);
  }

  async function submit() {
    // Same reasoning as the rack paste below: `busy` disables the button a render too late, and a
    // double-click posts the logger files twice.
    if (receiptRef.current) return;
    receiptRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.recordReceipt(
        view.case_id,
        {
          package_condition: condition,
          condition_notes: notes,
          package_count_received: count,
          refrigerant_condition: refrigerant,
          temperature_at_reception_c: tempC === "" ? null : Number(tempC),
          seal_intact: seal,
          logger_files: files,
        }
      );
      onDone();
    } catch (e) {
      setError(String(e));
      receiptRef.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4">
      <Card title="Condition on arrival">
        <p className="mb-3 text-sm text-fg-muted">
          Recorded once, kept forever. Condition at receipt is a permanent property of these specimens, a
          downstream researcher needs to know the box was warm even if every check later passes.
        </p>
        <div className="grid gap-2 sm:grid-cols-3">
          {CONDITIONS.map((c) => (
            <button
              key={c.value}
              onClick={() => setCondition(c.value)}
              className={`rounded border px-3 py-2 text-left transition ${
                condition === c.value ? "border-accent bg-info-bg" : "border-border bg-surface hover:bg-surface-2"
              }`}
            >
              <div className="text-sm font-medium text-fg">{c.label}</div>
              <div className="mt-0.5 text-[13px] leading-snug text-fg-muted">{c.hint}</div>
            </button>
          ))}
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <label className="text-sm text-fg-muted">
            Containers received
            <input
              type="number"
              min={0}
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
              className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
            />
            <span className="mt-1 block text-[13px] text-fg-subtle">{declared} declared</span>
          </label>
          <label className="text-sm text-fg-muted">
            Refrigerant on opening
            <input
              value={refrigerant}
              onChange={(e) => setRefrigerant(e.target.value)}
              placeholder="e.g. dry ice remaining, ~2 kg"
              className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
            />
          </label>
          <label className="text-sm text-fg-muted">
            Temperature at reception (°C)
            <input
              type="number"
              step="0.1"
              value={tempC}
              onChange={(e) => setTempC(e.target.value)}
              placeholder="optional"
              className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
            />
          </label>
        </div>

        <label className="mt-4 flex items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={seal} onChange={(e) => setSeal(e.target.checked)} className="size-4" />
          Tamper seal intact
        </label>

        {(condition !== "ACCEPTABLE" || !seal || count !== declared) && (
          <label className="mt-3 block text-sm text-fg-muted">
            What did you see?
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              placeholder="Describe the damage, the discrepancy, or the broken seal in the words you would use to a colleague."
              className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 text-sm text-fg"
            />
          </label>
        )}
      </Card>

      <Card title="Temperature loggers">
        <p className="mb-3 text-sm text-fg-muted">
          Drop the files straight off the loggers. They are read as evidence, not summarised by hand, the
          excursion analysis runs on every reading.
        </p>
        <input
          type="file"
          multiple
          accept=".csv,.txt,text/csv"
          onChange={(e) => readFiles(e.target.files)}
          className="block w-full text-sm text-fg-muted file:mr-3 file:rounded file:border-0 file:bg-accent file:px-3 file:py-1.5 file:text-sm file:text-accent-fg"
        />
        {files.length > 0 && (
          <ul className="mt-3 space-y-1 text-sm text-fg">
            {files.map((f) => (
              <li key={f.filename} className="flex items-center justify-between rounded bg-surface-2 px-2 py-1">
                <span className="font-mono text-sm">{f.filename}</span>
                <button onClick={() => setFiles((p) => p.filter((x) => x.filename !== f.filename))} className="text-sm text-fg-muted hover:text-fail-fg">
                  remove
                </button>
              </li>
            ))}
          </ul>
        )}
        {files.length === 0 && view.announcement?.logger_ids.length ? (
          <p className="mt-3 text-sm text-warn-fg">
            The site declared {view.announcement.logger_ids.length} logger(s): {view.announcement.logger_ids.join(", ")}. Without the
            files the temperature check cannot pass, and every specimen will need a human decision.
          </p>
        ) : null}
      </Card>

      <ErrorBox error={error} />
      <div className="flex justify-end gap-2">
        <Button onClick={submit} disabled={busy}>
          {busy ? "Recording…" : "Record receipt and start scanning"}
        </Button>
      </div>
    </div>
  );
}

/* ---- stage 2: the scanning grid ---------------------------------------------------------------- */

function ScanFeedback({ last }: { last: ScanOutcome | null }) {
  if (!last) {
    return (
      <div className="rounded border border-dashed border-border px-3 py-2 text-sm text-fg-subtle">
        Scan a tube, or type its identifier and press Enter.
      </div>
    );
  }
  const style = OUTCOME_STYLE[last.outcome] ?? OUTCOME_STYLE.MATCHED;
  const declared = last.outcome === "NEAR_MATCH" ? last.matched_sample_id : null;
  const difference = declared ? glyphDifference(declared, last.scanned_value) : null;
  return (
    <div role="status" aria-live="polite" className={`rounded border px-3 py-2 text-sm ${style.chip}`}>
      <span className="font-semibold">
        {declared ? <DiffedValue declared={declared} scanned={last.scanned_value} /> : <span className="font-mono">{last.scanned_value}</span>}
      </span>
      <span className="mx-2 opacity-60">·</span>
      <span className="font-medium">{style.label}</span>
      {last.message && <span className="ml-2 opacity-90">{last.message}</span>}
      {difference && <div className="mt-1 text-sm opacity-90">{difference}</div>}
    </div>
  );
}

function ScanBench({ view, onCommitted }: { view: IntakeView; onCommitted: (caseId: string) => void }) {
  const batch = view.batch as BatchSummary;
  const [rows, setRows] = useState<ExpectedRow[]>(batch.rows);
  const [summary, setSummary] = useState<BatchSummary>(batch);
  const [value, setValue] = useState("");
  const [last, setLast] = useState<ScanOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inFlight, setInFlight] = useState(0);
  const [acceptPartial, setAcceptPartial] = useState(false);
  // A specimen label routinely carries two codes, a linear tube ID and a 2D site accession, and they
  // are not interchangeable. The bench does not guess which was just read; the technician says.
  const [mode, setMode] = useState<"tube" | "accession">("tube");
  const [target, setTarget] = useState<ExpectedRow | null>(null);
  const [pasting, setPasting] = useState(false);
  // Read inside the scan queue's async callback, which would close over stale state otherwise.
  const pastingRef = useRef(false);
  const [paste, setPaste] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Scans are queued, never dropped. A handheld scanner fires reads faster than a round trip, and a read
  // discarded because the previous one was still in flight is a tube that silently never got recorded,
  // the tech saw it scan, so they will not scan it again.
  const queue = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submitDecoded = useCallback(
    (values: string[]) => {
      for (const value of values) {
        const v = value.trim();
        if (!v) continue;
        setInFlight((n) => n + 1);
        const scanMode = mode;
        const accessionRow = target?.row;
        queue.current = queue.current.then(async () => {
          try {
            if (scanMode === "accession") {
              if (accessionRow === undefined) {
                setError("Choose which row this accession belongs to first.");
                return;
              }
              const out = await api.attachAccession(view.case_id, accessionRow, v);
              setSummary(out.batch);
              setRows(out.batch.rows);
              setTarget(null);
              setError(null);
              return;
            }
            const out = await api.scan(view.case_id, { value: v });
            setLast(out);
            setSummary(out.batch);
            setRows(out.batch.rows);
            setError(null);
          } catch (e) {
            setError(`${v}: ${String(e)}`);
          } finally {
            setInFlight((n) => n - 1);
          }
        });
      }
    },
    [view.case_id, mode, target],
  );

  const submitScan = useCallback(() => {
    const v = value.trim();
    if (!v) return;
    setValue("");
    setInFlight((n) => n + 1);
    const scanMode = mode;
    const accessionRow = target?.row;
    queue.current = queue.current.then(async () => {
      try {
        if (scanMode === "accession") {
          if (accessionRow === undefined) {
            setError("Choose which row this accession belongs to first.");
            return;
          }
          const out = await api.attachAccession(view.case_id, accessionRow, v);
          setSummary(out.batch);
          setRows(out.batch.rows);
          setTarget(null);
          setError(null);
          return;
        }
        const out = await api.scan(view.case_id, { value: v });
        setLast(out);
        setSummary(out.batch);
        setRows(out.batch.rows);
        setError(null);
      } catch (e) {
        setError(`${v}: ${String(e)}`);
      } finally {
        setInFlight((n) => n - 1);
        if (!pastingRef.current) inputRef.current?.focus();
      }
    });
  }, [value, view.case_id, mode, target]);

  useEffect(() => {
    pastingRef.current = pasting;
  }, [pasting]);

  const submittingRef = useRef(false);

  async function submitPaste() {
    // Guarded on a ref, not on `busy`. setBusy schedules a re-render; it does not disable the
    // button before a second click lands in the same frame, and a double-click here records the
    // whole rack twice, every tube comes back "scanned twice", the near match is destroyed
    // because row 7 is already filled, and the commit count is wrong. A person doing this at a
    // bench with a hand scanner will double-click.
    if (submittingRef.current) return;
    submittingRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const out = await api.scanBulk(view.case_id, paste);
      setSummary(out.batch);
      setRows(out.batch.rows);
      setLast(out.results[out.results.length - 1] ?? null);
      setPaste("");
      setPasting(false);
    } catch (e) {
      setError(String(e));
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  }

  async function changeQuality(row: number, quality: string) {
    try {
      const out = await api.setQuality(view.case_id, row, quality);
      setSummary(out.batch);
      setRows(out.batch.rows);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  async function commit() {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await queue.current; // never commit while a scan is still in flight
      await api.commitBatch(view.case_id, acceptPartial);
      onCommitted(view.case_id);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  const missing = summary.not_scanned.length;
  const exceptions = summary.unexpected.length + summary.duplicates.length;

  return (
    <div className="space-y-4">
      <Card
        title="Scan every tube"
        right={
          <span className="text-sm text-fg-muted">
            {inFlight > 0 && <span className="mr-2 text-accent">{inFlight} recording…</span>}
            {summary.scanned} of {summary.expected}
          </span>
        }
      >
        <div className="flex flex-wrap items-end gap-6">
          <div className="min-w-[16rem] flex-1">
            <div className="mb-1 flex flex-wrap items-center gap-3">
              <label htmlFor="scan" className="text-sm uppercase tracking-wide text-fg-muted">
                Scanner input
              </label>
              <div role="radiogroup" aria-label="What are you scanning?" className="flex gap-1">
                {([
                  ["tube", "Tube ID"],
                  ["accession", "Site accession"],
                ] as const).map(([m, label]) => (
                  <button
                    key={m}
                    role="radio"
                    aria-checked={mode === m}
                    onClick={() => {
                      setMode(m);
                      setTarget(null);
                      inputRef.current?.focus();
                    }}
                    className={`rounded px-2 py-0.5 text-[13px] ${
                      mode === m ? "bg-accent text-accent-fg" : "border border-border text-fg-muted hover:bg-surface-2"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <input
              id="scan"
              ref={inputRef}
              value={value}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitScan();
                }
              }}
              // Keep the scanner field focused when focus goes nowhere, a stray click on the page
              // background, which is constant at a bench. But never steal it back from another control:
              // doing so makes the paste box and the condition dropdowns impossible to use.
              onBlur={(e) => {
                if (!e.relatedTarget) setTimeout(() => inputRef.current?.focus(), 0);
              }}
              placeholder="BX-201"
              className="mt-1 w-full rounded border-2 border-accent bg-surface px-3 py-2 font-mono text-lg text-fg focus:outline-none"
            />
          </div>
          <div className="flex gap-5">
            <Stat n={summary.matched} label="Matched" tone="pass" />
            <Stat n={summary.near_matches} label="Near match" tone="warn" />
            <Stat n={missing} label="Not scanned" tone="warn" />
            <Stat n={exceptions} label="Exceptions" tone="fail" />
          </div>
        </div>
        <div className="mt-3">
          {mode === "accession" ? (
            <div className="rounded border border-info-border bg-info-bg px-3 py-2 text-sm text-info-fg">
              {target ? (
                <>
                  Scanning the site accession for row {target.row} -{" "}
                  <span className="font-mono">{target.scanned_value ?? target.sample_id}</span>.{" "}
                  <button onClick={() => setTarget(null)} className="underline">
                    choose a different row
                  </button>
                </>
              ) : (
                <>
                  Pick the row this accession belongs to, from the Accession column below. The manifest does
                  not contain accessions, so this one is attached to a row rather than matched.
                </>
              )}
            </div>
          ) : (
            <ScanFeedback last={last} />
          )}
        </div>
        <div className="mt-3">
          {pasting ? (
            <div className="rounded border border-border bg-surface-2 p-3">
              <label className="text-sm text-fg-muted">
                Paste a column of identifiers, commas, tabs or line breaks
                <textarea
                  value={paste}
                  onChange={(e) => setPaste(e.target.value)}
                  rows={5}
                  spellCheck={false}
                  autoFocus
                  placeholder={"BX-201\nBX-202\nBX-203"}
                  className="mt-1 w-full rounded border border-border bg-surface px-2 py-1.5 font-mono text-sm text-fg"
                />
              </label>
              <p className="mt-1 text-[13px] text-fg-subtle">
                Every value goes through the same matching a handheld read gets; nothing is accepted just
                because it was pasted.
              </p>
              <div className="mt-2 flex items-center gap-2">
                <Button onClick={submitPaste} disabled={busy || !paste.trim()}>
                  {busy ? "Recording…" : `Record ${paste.split(/[\s,;\t]+/).filter(Boolean).length} identifier(s)`}
                </Button>
                <button onClick={() => setPasting(false)} className="text-sm text-fg-muted hover:text-fg">
                  cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <button onClick={() => setPasting(true)} className="text-sm text-accent hover:underline">
                Paste a whole rack instead
              </button>
              <BarcodeScanner
                onDecode={submitDecoded}
                label={mode === "accession" ? "Scan the accession with the camera" : "Scan with the camera"}
              />
            </div>
          )}
        </div>
        <ErrorBox error={error} />
      </Card>

      {exceptions > 0 && (
        <Card title="Exceptions">
          <ul className="space-y-1 text-sm">
            {summary.unexpected.map((v) => (
              <li key={`u-${v}`} className="flex items-center gap-2">
                <span className="rounded border border-fail-border bg-fail-bg px-1.5 py-0.5 text-[13px] text-fail-fg">Not on manifest</span>
                <span className="font-mono">{v}</span>
                <span className="text-fg-muted">- arrived but was never declared; the sender has to account for it.</span>
              </li>
            ))}
            {summary.duplicates.map((v) => (
              <li key={`d-${v}`} className="flex items-center gap-2">
                <span className="rounded border border-fail-border bg-fail-bg px-1.5 py-0.5 text-[13px] text-fail-fg">Duplicate</span>
                <span className="font-mono">{v}</span>
                <span className="text-fg-muted">- scanned twice, or two tubes carry the same identifier.</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Manifest" right={<span className="text-sm text-fg-subtle">The site declared these rows; you are filling one column</span>}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[46rem] text-sm">
            <thead>
              <tr className="border-b border-border text-left text-[13px] uppercase tracking-wide text-fg-muted">
                <th className="py-1.5 pr-2 font-medium">Row</th>
                <th className="py-1.5 pr-2 font-medium">Declared</th>
                <th className="py-1.5 pr-2 font-medium">Participant</th>
                <th className="py-1.5 pr-2 font-medium">Type</th>
                <th className="py-1.5 pr-2 font-medium">Container</th>
                <th className="py-1.5 pr-2 font-medium">Scanned</th>
                <th className="py-1.5 pr-2 font-medium">Accession</th>
                <th className="py-1.5 pr-2 font-medium">Condition</th>
                <th className="py-1.5 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const style = r.outcome ? OUTCOME_STYLE[r.outcome] : null;
                return (
                  <tr key={r.row} className={`border-b border-border/60 ${style?.row ?? ""}`}>
                    <td className="py-1.5 pr-2 tabular-nums text-fg-subtle">{r.row}</td>
                    <td className="py-1.5 pr-2 font-mono text-fg">{r.sample_id}</td>
                    <td className="py-1.5 pr-2 font-mono text-sm text-fg-muted">{r.participant_reference}</td>
                    <td className="py-1.5 pr-2 text-fg-muted">{r.specimen_type}</td>
                    <td className="py-1.5 pr-2 text-fg-muted">{r.container_id}</td>
                    <td className="py-1.5 pr-2 text-fg">
                      {r.scanned_value === null ? (
                        <span className="font-mono text-fg-subtle">-</span>
                      ) : r.outcome === "NEAR_MATCH" ? (
                        <>
                          <DiffedValue declared={r.sample_id} scanned={r.scanned_value} />
                          {glyphDifference(r.sample_id, r.scanned_value) && (
                            <div className="mt-0.5 text-[13px] font-normal text-warn-fg">{glyphDifference(r.sample_id, r.scanned_value)}</div>
                          )}
                        </>
                      ) : (
                        <span className="font-mono">{r.scanned_value}</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2">
                      {r.scanned_value === null ? (
                        <span className="text-[13px] text-fg-subtle">-</span>
                      ) : r.encoded_barcode ? (
                        <span className="font-mono text-[13px] text-fg-muted">{r.encoded_barcode}</span>
                      ) : mode === "accession" ? (
                        <button
                          onClick={() => {
                            setTarget(r);
                            inputRef.current?.focus();
                          }}
                          className={`rounded border px-1.5 py-0.5 text-[13px] ${
                            target?.row === r.row
                              ? "border-accent bg-info-bg text-info-fg"
                              : "border-border text-fg-muted hover:bg-surface-2"
                          }`}
                        >
                          {target?.row === r.row ? "scanning…" : "attach"}
                        </button>
                      ) : (
                        <span className="text-[13px] text-fg-subtle">-</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2">
                      {r.scanned_value === null ? (
                        <span className="text-[13px] text-fg-subtle">-</span>
                      ) : (
                        <select
                          aria-label={`Received condition for ${r.sample_id}`}
                          value={r.received_quality ?? "ACCEPTABLE"}
                          onChange={(e) => void changeQuality(r.row, e.target.value)}
                          className={`rounded border px-1 py-0.5 text-[13px] ${
                            (r.received_quality ?? "ACCEPTABLE") === "ACCEPTABLE"
                              ? "border-border bg-surface text-fg-muted"
                              : "border-warn-border bg-warn-bg text-warn-fg"
                          }`}
                        >
                          {QUALITIES.map((q) => (
                            <option key={q.value} value={q.value}>
                              {q.label}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="py-1.5">
                      {style ? (
                        <span className={`rounded border px-1.5 py-0.5 text-[13px] ${style.chip}`}>{style.label}</span>
                      ) : (
                        <span className="text-[13px] text-fg-subtle">awaiting</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-surface px-4 py-3 shadow-[var(--shadow)]">
        <div className="text-sm">
          {missing === 0 ? (
            <span className="text-pass-fg">All {summary.expected} declared specimens accounted for.</span>
          ) : (
            <label className="flex items-center gap-2 text-warn-fg">
              <input type="checkbox" checked={acceptPartial} onChange={(e) => setAcceptPartial(e.target.checked)} className="size-4" />
              <span>
                {missing} not scanned ({summary.not_scanned.slice(0, 4).join(", ")}
                {missing > 4 ? "…" : ""}). Commit as a <strong>partial receipt</strong>.
              </span>
            </label>
          )}
        </div>
        <Button onClick={commit} disabled={busy || inFlight > 0 || summary.scanned === 0 || (missing > 0 && !acceptPartial)}>
          {busy ? "Committing…" : `Commit ${summary.scanned} specimen${summary.scanned === 1 ? "" : "s"}`}
        </Button>
      </div>
    </div>
  );
}

/* ---- the screen -------------------------------------------------------------------------------- */

export function ReceivingBench({ caseId }: { caseId: string }) {
  const [view, setView] = useState<IntakeView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setView(await api.intake(caseId));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [caseId]);

  useEffect(() => {
    const t = setTimeout(refresh, 0);
    return () => clearTimeout(t);
  }, [refresh]);

  if (error && !view) {
    return (
      <>
        <PageHeader title="Receiving bench" />
        <PageError error={error} onRetry={refresh} backHref="/receive" backLabel="Back to receiving" />
      </>
    );
  }
  if (!view) {
    return (
      <>
        <PageHeader title="Receiving bench" meta="Loading the shipment…" />
        <div className="w-full space-y-4 p-4 sm:p-5">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-80 w-full" />
        </div>
      </>
    );
  }

  const committed = Boolean(view.batch?.committed_at);
  const ann = view.announcement;

  return (
    <>
      <PageHeader
        title={ann ? ann.shipment_id : view.case_id}
        badge={<StateBadge state={view.state} />}
        meta={
          ann ? (
            <>
              from {ann.sender_site_id} · {ann.courier || "no courier recorded"}
              {ann.tracking_reference && <span className="font-mono"> {ann.tracking_reference}</span>} ·{" "}
              {ann.expected_lines.length} specimens declared in {ann.container_count} container
              {ann.container_count === 1 ? "" : "s"}
            </>
          ) : (
            view.case_id
          )
        }
      />
      <div className="w-full space-y-4 p-4 sm:p-5">

      {!ann && (
        <Card>
          <p className="text-sm text-fg-muted">
            This case has no advance notification, so there is nothing to receive against. It was created
            directly rather than announced by a site.
          </p>
        </Card>
      )}

      {ann && !view.receipt && <ReceiptForm view={view} onDone={refresh} />}

      {ann && view.receipt && !committed && view.batch && <ScanBench view={view} onCommitted={() => refresh()} />}

      {committed && (
        <Card title="Batch committed">
          <p className="text-sm text-fg-muted">
            {view.batch?.scanned} specimen(s) were committed and now exist as records. Verification is the
            agent&rsquo;s work from here: it reconciles them against the protocol, consent, temperature and LIMS,
            and brings anything it cannot settle to a person.
          </p>
          <div className="mt-3">
            <Link href={`/cases/${caseId}?run=1`} className="inline-block rounded bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg hover:brightness-110">
              Open the case workspace
            </Link>
          </div>
        </Card>
      )}
      </div>
    </>
  );
}
