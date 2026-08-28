"use client";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

// The signed-in session. Who you are is decided by the server from this token; the app cannot
// choose a role for itself, which is the point, every decision in the audit trail is attributed.
const TOKEN_KEY = "biointake.token";

export type Session = { user_id: string; display_name: string; role: string };
/** Only served where the deployment has been asked to offer one-click sign-in for review. */
export type DemoIdentity = { user_id: string; display_name: string; role: string; token: string };

export function currentToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

const sessionListeners = new Set<() => void>();

export function subscribeSession(listener: () => void): () => void {
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

export function setToken(token: string) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
  sessionListeners.forEach((l) => l());
}

export function signOut() {
  setToken("");
}

/** FastAPI answers a validation failure with a list of {loc, msg}. Rendered naively that reaches
 *  the screen as "[object Object]", so each one is turned back into the field it is about. */
function readDetail(body: unknown): string {
  const detail = (body as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const e = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(e.loc) ? e.loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${e.msg ?? "invalid"}` : (e.msg ?? "invalid");
      })
      .join("; ");
  }
  return detail ? JSON.stringify(detail) : JSON.stringify(body);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as Record<string, string>) };
  const token = currentToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = readDetail(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

// ---- types (mirror the control API) ---------------------------------------------------------
export type CaseSummary = { case_id: string; shipment_id: string; state: string; samples: number; declared: number | null; active_requests: number; pending_decisions: number; updated_at: string };
export type Blocker = { category: string; status: string; reason_codes: string[]; observed: string | null };
export type SnapshotSample = { sample_id: string; state: string; checks: Record<string, string>; blockers: Blocker[] };
export type Snapshot = { case_id: string; state: string; case_version: number; samples: SnapshotSample[]; active_requests: string[]; pending_decisions: string[]; unresolved_requirements: { requirement_type: string; sample_id: string; description: string }[] };
export type ReportSample = { sample_id: string; state: string; disposition: string | null; checks: Record<string, string | null>; provisional_checks: string[]; evidence_refs: string[]; lims: { record_id: string; status: string; policy_evaluation_id: string | null } | null };
export type Report = {
  case_id: string; shipment_id: string; protocol: string; policy: string; case_state: string; received_at: string; created_at: string;
  counts: Record<string, number>; unauthorized_acceptances: number; samples: ReportSample[];
  evidence_requests: { request_id: string; recipient: string; status: string; requirements: string[] }[];
  human_decisions: { decision_id: string; sample_id: string; actor_id: string; actor_role: string; selected_option: string; comment: string; created_at: string }[];
  policy_evaluations: number; audit_counts_by_kind: Record<string, number>;
  audit_events: { seq: number; kind: string; type: string; actor: string; summary: string; reason_codes: string[] }[];
};
export type CheckDetail = {
  sample_id: string; category: string; status: string; summary: string;
  observed_value: string | null; expected_value: string | null;
  reason_codes: string[]; evidence_refs: string[]; provisional: boolean;
  rule_version: string; evaluated_at: string;
};
export type CaseView = { snapshot: Snapshot; report: Report; checks: CheckDetail[]; agent_running: boolean };
export type TemperatureSeries = {
  case_id: string;
  sample_id: string | null;
  permitted: { min_c: number; max_c: number; tolerance_minutes: number };
  loggers: {
    logger_id: string;
    artifact_id: string;
    reading_count: number;
    malformed_rows: number;
    downsampled_to: number;
    series: { t: string; c: number; out: boolean }[];
    metrics: { peak_c: number | null; min_c: number | null; cumulative_minutes_out: number; longest_continuous_minutes: number; largest_gap_minutes: number };
    status: string;
    reason_codes: string[];
    summary: string;
  }[];
};
export type EventsPage = { events: AuditEvent[]; agent_running: boolean; case_state: string };
export type AuditEvent = { audit_event_id: string; sequence: number; event_type: string; kind: string; actor_type: string; actor_id: string; summary: string; tool_name: string | null; output_status: string; reason_codes: string[]; sample_ids: string[]; timestamp: string; metadata: Record<string, unknown> };
export type RunResult = {
  case_id: string; event_id: string; stable_state: string; is_stable: boolean; stop_reason: string;
  committed_dispositions: Record<string, string>; created_evidence_request_ids: string[];
  pending_interrupt: { interrupt_id: string; name: string; reason: Record<string, unknown> } | null;
  checks_evaluated: number; checks_reverified: number; tool_attempt_count: number; logical_effect_count: number;
  retry_count: number; model_call_count: number; intervention_denials: number; unauthorized_acceptances: number; warnings: string[]; final_text: string; boot_id: string;
};
export type OutboxMessage = { request_id: string; status: string; to: { contact_id: string; display_name: string | null; destination: string | null }; subject: string; body: string; sent_at: string; portal_path: string; affected_sample_ids: string[] };
export type EvidenceRequestView = { request_id: string; case_id: string; status: string; recipient: { contact_id: string; display_name: string | null }; subject: string; body: string; requirements: { requirement_type: string; sample_id: string; description: string }[]; affected_sample_ids: string[]; sent_at: string; expires_at: string };
export type DecisionOption = { option: string; required_roles: string[]; consequence: string };
export type PendingDecision = { issue_id: string; case_id: string; sample_id: string; issue_type: string; observed_value: string; expected_value: string; policy_clause: string; evidence_refs: string[]; passed_checks: string[]; blocked_checks: string[]; options: DecisionOption[]; created_at: string; interrupt_id: string | null; resolved_decision_id: string | null };


// ---- the lab's own configuration: the sites it writes to, the studies it receives against ------
export type Contact = { contact_id: string; site_id: string; display_name: string; destination: string; shipment_ids: string[]; role: string; active: boolean };

// ---- the intake ramp: announcement → receipt → scanning → commit ------------------------------
export type Policy = {
  policy_id: string; version: string; protocol_id: string; title: string;
  allowed_specimen_types: string[]; required_checks: string[];
  temperature: { min_c: number; max_c: number; tolerance_minutes: number; max_gap_minutes: number; exception_allowed: boolean; exception_roles: string[]; clause: string };
  consent: { min_version: number; required_scope: string; clause: string };
  custody: { required_events: string[]; clause: string };
  quarantine_roles: string[]; reject_roles: string[];
};
export type Study = { study_id: string; name: string; protocol_id: string; policy_version: string; policy: Policy; site_ids: string[]; relabelling_permitted: boolean; exception_approval_role: string; reconcile_before_storage: boolean };
export type ManifestLine = { row: number; sample_id: string; participant_reference: string; specimen_type: string; container_id: string; collection_timestamp: string | null; notes: string };
export type ManifestValidation = { accepted: boolean; summary: string; problems: string[]; warnings: string[]; reason_codes: string[]; lines: ManifestLine[] };
export type Announcement = { shipment_id: string; case_id: string; study_id: string; sender_site_id: string; announced_by_contact_id: string; courier: string; tracking_reference: string; shipped_at: string | null; expected_arrival: string | null; container_count: number; logger_ids: string[]; shipping_condition: string; manifest_artifact_id: string; expected_lines: ManifestLine[]; announced_at: string };
export type Receipt = { case_id: string; package_condition: string; condition_notes: string; package_count_received: number; refrigerant_condition: string; temperature_at_reception_c: number | null; seal_intact: boolean; logger_artifact_ids: string[]; received_by_actor_id: string; received_at: string };
export type ExpectedRow = { row: number; sample_id: string; participant_reference: string; specimen_type: string; container_id: string; notes: string; scanned_value: string | null; outcome: string | null; received_quality: string | null; encoded_barcode: string | null; scanned_at: string | null };
export type BatchSummary = { batch_id: string | null; committed_at: string | null; expected: number; scanned: number; matched: number; near_matches: number; not_scanned: string[]; unexpected: string[]; duplicates: string[]; rows: ExpectedRow[] };
export type IntakeView = { case_id: string; state: string; announcement: Announcement | null; receipt: Receipt | null; batch: BatchSummary | null };
export type ScanOutcome = { scan_id: string; outcome: string; matched_row: number | null; matched_sample_id: string | null; scanned_value: string; message: string; batch: BatchSummary };

export type VerificationReport = {
  case_id: string; shipment_id: string; complete: boolean; case_state: string;
  clauses: Record<string, string>;
  receipt: { sending_site: string | null; announced_by: string | null; courier: string; tracking_reference: string; received_at: string | null; received_by: string | null };
  condition: {
    package_condition: string | null; condition_notes: string; seal_intact: boolean | null;
    refrigerant_condition: string; temperature_at_reception_c: number | null;
    containers_declared: number | null; containers_received: number | null; container_count_matched: boolean;
    logger_files_received: number;
    specimen_condition_notes: { sample_id: string; received_quality: string }[];
  };
  reconciliation: {
    declared: number; received: number; matched: number; manifest_fully_reconciled: boolean;
    not_received: string[]; not_on_manifest: string[]; duplicate_identifiers: string[];
    identifier_near_matches: { row: number; declared: string; read_on_tube: string }[];
  };
  resolutions: { sample_id: string; resolution: string; settled_by: string; comment: string; at: string }[];
  disposition: { accepted: string[]; accepted_with_exception: string[]; held: string[]; rejected: string[]; still_open: string[]; policy: string };
};

// ---- calls ------------------------------------------------------------------------------------
export const api = {
  reopenQuarantine: (caseId: string, sampleId: string, reason: string) =>
    call<{ status: string; summary: string; sample: { state: string }; case_state: string }>(
      `/api/cases/${caseId}/samples/${sampleId}/quarantine-review`,
      { method: "POST", body: JSON.stringify({ reason }) }
    ),
  verificationReport: (id: string) => call<VerificationReport>(`/api/cases/${id}/verification-report`),
  studies: () => call<Study[]>("/api/studies"),
  validateManifest: (studyId: string, csvBase64: string) =>
    call<ManifestValidation>("/api/manifests/validate", { method: "POST", body: JSON.stringify({ study_id: studyId, manifest_csv_base64: csvBase64 }) }),
  announce: (body: Record<string, unknown>) =>
    call<{ case_id: string; state: string; announcement: Announcement; declared_specimens: number }>("/api/shipments/announce", { method: "POST", body: JSON.stringify(body) }),
  intake: (id: string) => call<IntakeView>(`/api/cases/${id}/intake`),
  recordReceipt: (id: string, body: Record<string, unknown>) =>
    call<Receipt>(`/api/cases/${id}/receipt`, { method: "POST", body: JSON.stringify(body) }),
  scanBulk: (id: string, text: string) =>
    call<{ scanned: number; results: ScanOutcome[]; batch: BatchSummary }>(`/api/cases/${id}/scan/bulk`, { method: "POST", body: JSON.stringify({ text }) }),
  attachAccession: (id: string, row: number, encodedBarcode: string) =>
    call<{ row: number; batch: BatchSummary }>(`/api/cases/${id}/accession`, { method: "POST", body: JSON.stringify({ row, encoded_barcode: encodedBarcode }) }),
  setQuality: (id: string, row: number, quality: string) =>
    call<{ row: number; received_quality: string; batch: BatchSummary }>(`/api/cases/${id}/quality`, { method: "POST", body: JSON.stringify({ row, received_quality: quality }) }),
  scan: (id: string, body: { value: string; container_id?: string; encoded_barcode?: string }) =>
    call<ScanOutcome>(`/api/cases/${id}/scan`, { method: "POST", body: JSON.stringify(body) }),
  commitBatch: (id: string, acceptPartial: boolean) =>
    call<{ committed: string[]; summary: BatchSummary; state: string }>(`/api/cases/${id}/batch/commit`, { method: "POST", body: JSON.stringify({ accept_partial: acceptPartial }) }),
  me: () => call<Session>("/api/me"),
  demoIdentities: () => call<DemoIdentity[]>("/api/demo/identities"),
  contacts: (shipmentId?: string) =>
    call<Contact[]>(`/api/contacts${shipmentId ? `?shipment_id=${encodeURIComponent(shipmentId)}` : ""}`),
  createContact: (body: Omit<Contact, "role" | "active">) =>
    call<Contact>("/api/contacts", { method: "POST", body: JSON.stringify(body) }),
  createStudy: (body: { study_id: string; name: string; policy: Record<string, unknown>; site_ids: string[] }) =>
    call<Study>("/api/studies", { method: "POST", body: JSON.stringify(body) }),
  listCases: () => call<CaseSummary[]>("/api/cases"),
  getCase: (id: string) => call<CaseView>(`/api/cases/${id}`),
  events: (id: string, after = 0) => call<EventsPage>(`/api/cases/${id}/events?after=${after}`),
  outbox: (id: string) => call<OutboxMessage[]>(`/api/cases/${id}/outbox`),
  decisions: (id: string) => call<PendingDecision[]>(`/api/cases/${id}/decisions`),
  temperature: (id: string, sampleId?: string) => call<TemperatureSeries>(`/api/cases/${id}/temperature${sampleId ? `?sample_id=${encodeURIComponent(sampleId)}` : ""}`),
  demoReset: () => call<{ case_id: string }>("/api/demo/reset", { method: "POST" }),
  demoLoad: () => call<{ case_id: string; session_id: string; state: string; created: boolean }>("/api/demo/load", { method: "POST" }),
  demoSenderReply: () => call<{ from_contact_id: string; free_text: string; files: { filename: string; mime_type: string; content_base64: string }[] }>("/api/demo/sender-reply"),
  run: (id: string) => call<RunResult>(`/api/cases/${id}/run`, { method: "POST", body: JSON.stringify({ event_type: "CASE_READY" }) }),
  getRequest: (rid: string) => call<EvidenceRequestView>(`/api/evidence-requests/${rid}`),
  completeRequest: (rid: string, body: { upload_token: string; submitted_by_contact_id: string; sender_message: string; files: { filename: string; mime_type: string; content_base64: string }[] }) =>
    call<RunResult>(`/api/evidence-requests/${rid}/complete`, { method: "POST", body: JSON.stringify(body) }),
  respond: (id: string, interruptId: string, body: { selected_option: string; comment: string }) =>
    call<RunResult>(`/api/cases/${id}/interrupts/${interruptId}/respond`, { method: "POST", body: JSON.stringify(body) }),
};

/** Poll faster while the agent is working, slowly when the case is at rest. */
export function pollInterval(active: boolean): number {
  return active ? 600 : 4000;
}

export const CHECKS = ["IDENTITY_MATCH", "MANIFEST_MATCH", "PROTOCOL_ELIGIBILITY", "CONSENT_VALIDITY", "TEMPERATURE_REQUIREMENT", "CHAIN_OF_CUSTODY", "LIMS_RECONCILIATION"] as const;
export const CHECK_LABELS: Record<string, string> = { IDENTITY_MATCH: "Identity", MANIFEST_MATCH: "Manifest", PROTOCOL_ELIGIBILITY: "Protocol", CONSENT_VALIDITY: "Consent", TEMPERATURE_REQUIREMENT: "Temperature", CHAIN_OF_CUSTODY: "Custody", LIMS_RECONCILIATION: "LIMS" };
