"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Contact, type Policy, type Session, type Study } from "@/lib/api";
import { Button, Card, ErrorBox, Skeleton } from "@/components/ui";

/* ------------------------------------------------------------------------------------------------
   The lab's own configuration: the sites it is willing to write to, and the studies it receives
   against. Both existed only as API routes, which meant a new lab could not start receiving without
   somebody running curl. Neither is shipment data; it is there before any box arrives.
------------------------------------------------------------------------------------------------ */

const AUTHORING_ROLES = ["PRINCIPAL_INVESTIGATOR", "QA_REVIEWER"];

function Field({
  label,
  hint,
  value,
  onChange,
  placeholder = "",
  type = "text",
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="block text-sm">
      <span className="text-fg-muted">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded border border-border bg-bg px-2 py-1.5 text-sm text-fg"
      />
      {hint ? <span className="mt-1 block text-[13px] text-fg-muted">{hint}</span> : null}
    </label>
  );
}

// ---- site contacts -----------------------------------------------------------------------------

function ContactSection({ onChanged }: { onChanged: () => void }) {
  const [contacts, setContacts] = useState<Contact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ contact_id: "", site_id: "", display_name: "", destination: "" });

  const load = useCallback(() => {
    api
      .contacts()
      .then(setContacts)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(load, [load]);

  const complete = Object.values(form).every((v) => v.trim());

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.createContact({ ...form, shipment_ids: [] });
      setForm({ contact_id: "", site_id: "", display_name: "", destination: "" });
      load();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Site contacts">
      <div className="space-y-4 p-4">
        <p className="text-sm text-fg-muted">
          The only addresses BioIntake will write to. The agent may choose among these by id; it can
          never supply an address of its own, which is why adding one is a deliberate act by a person.
        </p>

        {contacts === null ? (
          <Skeleton className="h-20 w-full" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-fg-muted">
                  <th className="py-1 pr-4 font-medium">Contact</th>
                  <th className="py-1 pr-4 font-medium">Site</th>
                  <th className="py-1 pr-4 font-medium">Writes to</th>
                  <th className="py-1 font-medium">Shipments</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((c) => (
                  <tr key={c.contact_id} className="border-t border-border">
                    <td className="py-1.5 pr-4">
                      {c.display_name} <span className="font-mono text-fg-muted">{c.contact_id}</span>
                    </td>
                    <td className="py-1.5 pr-4 font-mono">{c.site_id}</td>
                    <td className="py-1.5 pr-4">{c.destination}</td>
                    <td className="py-1.5 font-mono text-fg-muted">
                      {c.shipment_ids.length ? c.shipment_ids.join(", ") : "none yet"}
                    </td>
                  </tr>
                ))}
                {contacts.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-3 text-fg-muted">
                      No sites registered. Until one is, a shipment that arrives short of paperwork has
                      nobody the agent is allowed to ask.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}

        <div className="grid gap-3 border-t border-border pt-4 sm:grid-cols-2">
          <Field
            label="Contact id"
            value={form.contact_id}
            placeholder="SITE-CONTACT-014"
            onChange={(v) => setForm({ ...form, contact_id: v })}
          />
          <Field
            label="Site id"
            value={form.site_id}
            placeholder="SITE-KUMASI"
            onChange={(v) => setForm({ ...form, site_id: v })}
          />
          <Field
            label="Name"
            value={form.display_name}
            placeholder="Yaa Mensimah (site coordinator)"
            onChange={(v) => setForm({ ...form, display_name: v })}
          />
          <Field
            label="Email"
            hint="Verify this out of band before adding it. BioIntake will send evidence requests here."
            value={form.destination}
            onChange={(v) => setForm({ ...form, destination: v })}
          />
        </div>
        <ErrorBox error={error} />
        <Button onClick={submit} disabled={busy || !complete}>
          {busy ? "Adding…" : "Add site contact"}
        </Button>
      </div>
    </Card>
  );
}

// ---- studies -----------------------------------------------------------------------------------

function policyFrom(
  base: Policy,
  form: { protocol_id: string; title: string; min_c: string; max_c: string; tolerance: string; consent_version: string; specimen_types: string }
): Policy {
  return {
    ...base,
    policy_id: `POLICY-${form.protocol_id}`,
    version: "1.0.0",
    protocol_id: form.protocol_id,
    title: form.title,
    allowed_specimen_types: form.specimen_types
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
    temperature: {
      ...base.temperature,
      min_c: Number(form.min_c),
      max_c: Number(form.max_c),
      tolerance_minutes: Number(form.tolerance),
    },
    consent: { ...base.consent, min_version: Number(form.consent_version) },
  };
}

function StudySection({ session, reloadKey }: { session: Session | null; reloadKey: number }) {
  const [studies, setStudies] = useState<Study[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({
    study_id: "",
    protocol_id: "",
    title: "",
    min_c: "2",
    max_c: "8",
    tolerance: "10",
    consent_version: "3",
    specimen_types: "PLASMA",
  });

  const load = useCallback(() => {
    api
      .studies()
      .then(setStudies)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(load, [load, reloadKey]);

  const mayAuthor = session !== null && AUTHORING_ROLES.includes(session.role);
  const base = studies?.[0]?.policy ?? null;
  const complete = form.study_id.trim() && form.protocol_id.trim() && form.title.trim();

  async function submit() {
    if (!base) return;
    setBusy(true);
    setError(null);
    try {
      await api.createStudy({
        study_id: form.study_id.trim(),
        name: form.title.trim(),
        policy: policyFrom(base, form) as unknown as Record<string, unknown>,
        site_ids: [],
      });
      setOpen(false);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Studies"
      right={
        mayAuthor ? (
          <Button variant="ghost" onClick={() => setOpen(!open)}>
            {open ? "Cancel" : "Author a study"}
          </Button>
        ) : null
      }
    >
      <div className="space-y-4 p-4">
        <p className="text-sm text-fg-muted">
          A study is the acceptance criteria a specimen is judged against, and every case records the
          version it was decided under. ISO 20387 §7.3.2.2 asks a biobank to define these and verify
          them on reception.
        </p>

        {studies === null ? (
          <Skeleton className="h-20 w-full" />
        ) : (
          <ul className="space-y-2">
            {studies.map((s) => (
              <li key={s.study_id} className="rounded border border-border p-3 text-sm">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-mono font-semibold text-fg">{s.study_id}</span>
                  <span className="text-fg">{s.name}</span>
                  <span className="font-mono text-fg-muted">
                    {s.policy.policy_id}@{s.policy_version}
                  </span>
                </div>
                <div className="mt-1 text-fg-muted">
                  {s.policy.temperature.min_c}–{s.policy.temperature.max_c} °C, tolerance{" "}
                  {s.policy.temperature.tolerance_minutes} min · consent ≥ v
                  {s.policy.consent.min_version} · {s.policy.allowed_specimen_types.join(", ")} ·{" "}
                  {s.policy.required_checks.length} required checks
                </div>
              </li>
            ))}
          </ul>
        )}

        {!mayAuthor ? (
          <p className="text-[13px] text-fg-muted">
            Authoring a study is reserved to a principal investigator or a QA reviewer, because it
            defines what may be accepted.
          </p>
        ) : null}

        {open && base ? (
          <div className="space-y-3 border-t border-border pt-4">
            <p className="text-[13px] text-fg-muted">
              Starts from {studies?.[0]?.study_id}&apos;s policy and changes what differs. The clauses,
              required checks, custody events and approval roles carry over unchanged.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Study id" value={form.study_id} placeholder="NORTH-01" onChange={(v) => setForm({ ...form, study_id: v })} />
              <Field label="Protocol id" value={form.protocol_id} placeholder="NORTH-01" onChange={(v) => setForm({ ...form, protocol_id: v })} />
              <Field label="Title" value={form.title} placeholder="Northstar longitudinal cohort" onChange={(v) => setForm({ ...form, title: v })} />
              <Field label="Allowed specimen types" hint="Comma separated" value={form.specimen_types} onChange={(v) => setForm({ ...form, specimen_types: v })} />
              <Field label="Minimum °C" type="number" value={form.min_c} onChange={(v) => setForm({ ...form, min_c: v })} />
              <Field label="Maximum °C" type="number" value={form.max_c} onChange={(v) => setForm({ ...form, max_c: v })} />
              <Field label="Excursion tolerance (minutes)" type="number" value={form.tolerance} onChange={(v) => setForm({ ...form, tolerance: v })} />
              <Field label="Minimum consent version" type="number" value={form.consent_version} onChange={(v) => setForm({ ...form, consent_version: v })} />
            </div>
            <Button onClick={submit} disabled={busy || !complete}>
              {busy ? "Saving…" : "Author study"}
            </Button>
          </div>
        ) : null}

        <ErrorBox error={error} />
      </div>
    </Card>
  );
}

export function LabConfiguration() {
  const [session, setSession] = useState<Session | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
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
  }, []);

  return (
    <div className="space-y-4">
      <ContactSection onChanged={() => setReloadKey((n) => n + 1)} />
      <StudySection session={session} reloadKey={reloadKey} />
    </div>
  );
}
