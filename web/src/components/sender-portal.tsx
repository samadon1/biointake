"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type EvidenceRequestView, type RunResult } from "@/lib/api";
import { Button, ErrorBox, Skeleton, StateBadge } from "@/components/ui";
import { NearMatchExplainer } from "@/components/identifiers";
import { Logo } from "@/components/shell";

type Upload = { filename: string; mime_type: string; content_base64: string };

export function SenderPortal({ requestId, token }: { requestId: string; token: string }) {
  const [req, setReq] = useState<EvidenceRequestView | null>(null);
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<Upload[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getRequest(requestId)
      .then(setReq)
      .catch((e) => setError(String(e)));
  }, [requestId]);

  useEffect(() => {
    const t = setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  async function onFiles(list: FileList | null) {
    if (!list) return;
    const out: Upload[] = [];
    for (const f of Array.from(list)) {
      const buf = await f.arrayBuffer();
      let bin = "";
      new Uint8Array(buf).forEach((b) => (bin += String.fromCharCode(b)));
      out.push({ filename: f.name, mime_type: f.type || "application/octet-stream", content_base64: btoa(bin) });
    }
    setFiles(out);
  }


  async function submit() {
    if (!req || !token) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.completeRequest(requestId, { upload_token: token, submitted_by_contact_id: req.recipient.contact_id, sender_message: message, files }));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    // Always light, and always the full height of the window. This page is deliberately not themed with
    // the rest of the product: it is opened from an email by someone who does not work at the lab, and the
    // dark operations chrome would read as somebody else's software. It must also cover the viewport,
    // letting the app's dark background show beneath it looks like a broken page to exactly the person
    // whose trust the upload depends on.
    <main data-theme="light" className="min-h-dvh bg-[#f3f4f6] text-[#15181c]">
      <header className="flex items-center gap-2 border-b border-zinc-300 bg-white px-5 py-3 text-sm">
        <span className="text-[#0a5fd0]">
          <Logo size={20} />
        </span>
        <span className="font-semibold">BioIntake</span>
        <span className="text-zinc-500">· secure evidence upload · Northstar Research Site</span>
      </header>
      <div className="mx-auto w-full max-w-2xl space-y-4 p-4 sm:p-6">
        {!req && !error && (
          <div className="space-y-3">
            <Skeleton className="h-8 w-2/3 bg-zinc-300" />
            <Skeleton className="h-24 w-full bg-zinc-300" />
          </div>
        )}
        {!req && error && (
          <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900" role="alert">
            <p className="font-semibold">This link could not be opened.</p>
            <p className="mt-1">It may have expired, or the request has already been satisfied.</p>
            <button onClick={load} className="mt-2 rounded bg-red-700 px-3 py-1 text-sm text-white">
              Try again
            </button>
          </div>
        )}
        {req && !token && (
          <div className="rounded border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900" role="alert">
            <p className="font-semibold">This link is missing its access code.</p>
            <p className="mt-1">
              Please open the link exactly as it appears in the email from the lab; it carries a code that
              proves the upload came from you. Nothing you enter here could be accepted without it.
            </p>
          </div>
        )}
        {req && token && (
          <>
            <div>
              <h1 className="text-xl font-semibold">{req.subject}</h1>
              <p className="mt-1 text-sm text-zinc-600">
                For {req.recipient.display_name} · request {req.request_id} · <span className="uppercase">{req.status}</span>
              </p>
            </div>
            <section className="rounded-lg border border-zinc-300 bg-white p-4">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500">Requested items</h2>
              <ul className="mt-2 space-y-1 text-sm">
                {req.requirements.map((r) => (
                  <li key={r.requirement_type + r.sample_id}>
                    <span className="font-mono">{r.sample_id}</span>, <NearMatchExplainer text={r.description} />
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-sm text-zinc-500">No action is needed for any other sample in this shipment.</p>
            </section>
            {result ? (
              <section className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 text-sm">
                <div className="font-semibold text-emerald-900">Received, thank you.</div>
                <p className="mt-1 text-emerald-900">
                  The intake agent re-checked {result.checks_reverified} item(s) and the case is now <StateBadge state={result.stable_state} small />.
                </p>
              </section>
            ) : (
              <section className="space-y-3 rounded-lg border border-zinc-300 bg-white p-4">
                <label className="block text-sm">
                  <span className="text-zinc-700">Message (optional)</span>
                  <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={4} className="mt-1 w-full rounded border border-zinc-300 p-2 text-sm" placeholder="e.g. addendum attached; row 7 of the manifest is a typo…" />
                </label>
                <label className="block text-sm">
                  <span className="text-zinc-700">Files</span>
                  <input
                    type="file"
                    multiple
                    aria-label="Files to upload"
                    onChange={(e) => onFiles(e.target.files)}
                    className="mt-1 block w-full text-sm text-zinc-600 file:mr-3 file:rounded-md file:border-0 file:bg-[#0a5fd0] file:px-3 file:py-1.5 file:text-sm file:font-semibold file:text-white hover:file:brightness-110"
                  />
                  {/* The input prints one filename itself, and only says "n files" beyond that, so list them
                      here only when there is more than one and the control has stopped being useful. */}
                  {files.length > 1 && <div className="mt-1 text-sm text-zinc-600">{files.map((f) => f.filename).join(", ")}</div>}
                </label>
                <div className="flex items-center gap-3">
                  <Button onClick={submit} disabled={busy || req.status !== "ACTIVE" || (files.length === 0 && !message)}>
                    {busy ? "Uploading…" : "Upload securely"}
                  </Button>
                  {req.status !== "ACTIVE" && <span className="text-sm text-zinc-500">This request is already {req.status.toLowerCase()}.</span>}
                </div>
                <ErrorBox error={error} />
                <p className="text-[13px] text-zinc-500">This link is valid for this request only. Uploads are checked against the shipment before anything is accepted.</p>
              </section>
            )}
          </>
        )}
      </div>
    </main>
  );
}
