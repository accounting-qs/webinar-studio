const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${API_TOKEN}` };
}

function jsonHeaders(): Record<string, string> {
  return { ...authHeaders(), "Content-Type": "application/json" };
}

/* ── Calendar Blocker ──────────────────────────────────────────────────── */

export interface CalendarVariant {
  variant: "A" | "B" | "C";
  style: string;
  title: string;
  description: string;
}

export interface GenerateCalendarRequest {
  segment: string;
  sub_niche?: string;
  topic?: string;
  client_story?: string;
}

export interface GenerateCalendarResponse {
  variants: CalendarVariant[];
}

export async function generateCalendarBlocker(
  req: GenerateCalendarRequest
): Promise<GenerateCalendarResponse> {
  const res = await fetch(`${API_URL}/generate/calendar-event`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(req),
  });

  if (!res.ok) {
    if (res.status === 429) {
      throw new Error("Rate limit hit — wait a moment and try again.");
    }
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Generation failed");
  }

  return res.json();
}

/* ── Outreach: Types ───────────────────────────────────────────────────── */

export interface ApiBucket {
  id: string;
  name: string;
  industry: string | null;
  // Legit counts (raw minus blocklisted); raw counts available as *_raw
  total_contacts: number;
  remaining_contacts: number;
  total_contacts_raw?: number;
  remaining_contacts_raw?: number;
  blocklisted_total?: number;
  blocklisted_available?: number;
  countries: string[];
  emp_range: string | null;
  source_file: string | null;
  copies_count: { titles: number; descriptions: number };
  has_primary_title: boolean;
  has_primary_description: boolean;
  title_primary_picked: boolean;
  desc_primary_picked: boolean;
  created_at: string | null;
  // included when ?include=copies
  titles?: ApiCopy[];
  descriptions?: ApiCopy[];
}

export interface ApiCopy {
  id: string;
  bucket_id: string;
  copy_type: "title" | "description";
  variant_index: number;
  text: string;
  is_primary: boolean;
  ai_feedback: string | null;
  created_at: string | null;
  is_assigned?: boolean;
}

export interface ApiSender {
  id: string;
  name: string;
  total_accounts: number;
  send_per_account: number;
  days_per_webinar: number;
  color: string | null;
  display_order: number;
  is_active: boolean;
}

export interface ApiWebinar {
  id: string;
  number: number;
  /** Free-text A/B variant label, e.g. "Account A". null for the unique
   * row of a non-variant number. Two webinars with the same `number` must
   * have different `variant_label`s; one row per number may have null. */
  variant_label: string | null;
  /** ConnectorCredential.id of the WebinarGeek account this variant uses
   * for sync. null → use the credential row named 'default'. */
  webinargeek_credential_id: string | null;
  /** Optional link to the previous webinar whose WG broadcast supplies this
   * webinar's Nonjoiners (registrants who did not watch live). */
  nonjoiner_source_webinar_id: string | null;
  date: string;
  status: string;
  broadcast_id: string | null;
  main_title: string | null;
  registration_link: string | null;
  unsubscribe_link: string | null;
  assignment_count: number;
  total_volume: number;
  total_remaining: number;
  total_accounts: number;
}

export interface ApiAssignment {
  id: string;
  webinar_id: string;
  bucket: { id: string; name: string; industry: string | null } | null;
  sender: { id: string; name: string; color: string | null } | null;
  description: string | null;
  list_url: string | null;
  // Legit counts (raw minus blocklisted); raw counts available as *_raw
  volume: number;
  remaining: number;
  volume_raw?: number;
  remaining_raw?: number;
  blocklisted_total?: number;
  blocklisted_assigned?: number;
  gcal_invited: number;
  accounts_used: number;
  send_per_account: number | null;
  days: number | null;
  title_copy: ApiCopy | null;
  desc_copy: ApiCopy | null;
  countries_override: string | null;
  emp_range_override: string | null;
  is_nonjoiners: boolean;
  is_no_list_data: boolean;
  is_setup: boolean;
  source_type: string;
  source_upload_id: string | null;
  list_name: string | null;
  display_order: number;
  bucket_remaining?: number;
}

export interface ApiUpload {
  id: string;
  file_name: string;
  total_contacts: number;
  total_buckets: number;
  bucket_summary: Array<{ name: string; count: number; countries: string[]; empRanges: string[]; avgConfidence: number }> | null;
  status: string;
  progress: number;
  processed_rows: number;
  inserted_count: number;
  skipped_count: number;
  overwritten_count: number;
  error_message: string | null;
  created_at: string | null;
}

export interface UploadFileResponse {
  id: string;
  file_name: string;
  storage_path: string;
  total_rows: number;
  file_size: number;
  headers: string[];
  preview_rows: string[][];
}

export interface UploadStatusResponse {
  id: string;
  file_name: string;
  status: string;
  progress: number;
  total_rows: number;
  processed_rows: number;
  inserted_count: number;
  skipped_count: number;
  overwritten_count: number;
  error_message: string | null;
  bucket_summary: Array<{ name: string; count: number; countries: string[]; empRanges: string[]; avgConfidence: number }> | null;
}

/* ── Outreach: Buckets ─────────────────────────────────────────────────── */

export async function fetchBuckets(includeCopies = false): Promise<{ buckets: ApiBucket[] }> {
  const params = includeCopies ? "?include=copies" : "";
  const res = await fetch(`${API_URL}/outreach/buckets${params}`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch buckets");
  return res.json();
}

export async function createBucket(data: {
  name: string;
  industry?: string;
  total_contacts: number;
  remaining_contacts?: number;
  countries?: string[];
  emp_range?: string;
  source_file?: string;
}): Promise<ApiBucket> {
  const res = await fetch(`${API_URL}/outreach/buckets`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create bucket");
  return res.json();
}

export async function updateBucket(
  bucketId: string,
  data: Partial<{ name: string; industry: string; total_contacts: number; remaining_contacts: number; countries: string[]; emp_range: string }>
): Promise<ApiBucket> {
  const res = await fetch(`${API_URL}/outreach/buckets/${bucketId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to update bucket");
  }
  return res.json();
}

/* ── Outreach: Copies ──────────────────────────────────────────────────── */

export async function fetchBucketCopies(bucketId: string): Promise<{ bucket_id: string; titles: ApiCopy[]; descriptions: ApiCopy[] }> {
  const res = await fetch(`${API_URL}/outreach/buckets/${bucketId}/copies`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch copies");
  return res.json();
}

export async function generateCopies(
  bucketId: string,
  data: { copy_type: "title" | "description" | "both"; variant_count?: number }
): Promise<{ bucket_id: string; batch_id: string; titles: ApiCopy[]; descriptions: ApiCopy[] }> {
  const res = await fetch(`${API_URL}/outreach/buckets/${bucketId}/copies/generate`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to generate copies");
  return res.json();
}

/* ── Bulk (background) copy generation ─────────────────────────────────── */

export type CopyGenJobStatus = "pending" | "generating" | "done" | "failed";

export interface ApiCopyGenJob {
  id: string;
  bucket_id: string;
  copy_type: "title" | "description";
  status: CopyGenJobStatus;
  error_message?: string | null;
  variant_count?: number;
  created_at?: string | null;
  completed_at?: string | null;
}

export async function generateCopiesBulk(data: {
  bucket_ids: string[];
  copy_type: "title" | "description" | "both";
  variant_count?: number;
}): Promise<{ jobs: ApiCopyGenJob[] }> {
  const res = await fetch(`${API_URL}/outreach/buckets/copies/generate-bulk`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to start bulk generation");
  return res.json();
}

export async function fetchCopyGenerationStatus(): Promise<{ jobs: ApiCopyGenJob[] }> {
  const res = await fetch(`${API_URL}/outreach/buckets/copies/generation-status`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch generation status");
  return res.json();
}

export async function retryCopyGenerationJob(jobId: string): Promise<ApiCopyGenJob> {
  const res = await fetch(`${API_URL}/outreach/buckets/copies/generation-jobs/${jobId}/retry`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to retry generation job");
  return res.json();
}

/* ── Webinar list export (background CSV build) ───────────────────────── */

export type WebinarListExportStatus = "pending" | "processing" | "ready" | "failed";

export interface ApiWebinarListExportJob {
  id: string;
  webinar_id: string;
  status: WebinarListExportStatus;
  contact_count: number;
  error_message?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export async function startWebinarListExport(
  webinarId: string,
): Promise<ApiWebinarListExportJob> {
  const res = await fetch(
    `${API_URL}/outreach/webinars/${webinarId}/export-lists`,
    { method: "POST", headers: authHeaders() },
  );
  if (!res.ok) throw new Error("Failed to start export");
  return res.json();
}

export async function fetchLatestWebinarListExport(
  webinarId: string,
): Promise<ApiWebinarListExportJob | null> {
  const res = await fetch(
    `${API_URL}/outreach/webinars/${webinarId}/export-lists/latest`,
    { headers: authHeaders() },
  );
  if (!res.ok) throw new Error("Failed to fetch export status");
  const data = (await res.json()) as { job: ApiWebinarListExportJob | null };
  return data.job;
}

export async function fetchActiveWebinarListExports(): Promise<{
  jobs: ApiWebinarListExportJob[];
}> {
  const res = await fetch(`${API_URL}/outreach/webinars/export-lists/active`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch export jobs");
  return res.json();
}

export async function downloadWebinarListExport(
  webinarId: string,
  jobId: string,
  filename: string,
): Promise<void> {
  const res = await fetch(
    `${API_URL}/outreach/webinars/${webinarId}/export-lists/${jobId}/download`,
    { headers: authHeaders() },
  );
  if (!res.ok) throw new Error("Failed to download export");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/* ── Webinar contact release ──────────────────────────────────────────── */

export interface ReleaseContactsResponse {
  release_batch_id: string;
  released: number;
  not_found: string[];
  already_available: string[];
  out_of_scope?: string[];
  by_status: { assigned: number; used: number };
  bucket_updates: Record<string, number>;
}

export async function releaseWebinarContacts(
  webinarId: string,
  emails: string[],
  releaseBatchId?: string,
): Promise<ReleaseContactsResponse> {
  const res = await fetch(
    `${API_URL}/outreach/webinars/${webinarId}/releases`,
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        emails,
        ...(releaseBatchId ? { release_batch_id: releaseBatchId } : {}),
      }),
    },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to release contacts");
  }
  return res.json();
}

export async function releaseContactsById(
  contactIds: string[],
  assignmentIds: string[],
  releaseBatchId?: string,
): Promise<ReleaseContactsResponse> {
  const res = await fetch(`${API_URL}/outreach/contacts/releases`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({
      contact_ids: contactIds,
      assignment_ids: assignmentIds,
      ...(releaseBatchId ? { release_batch_id: releaseBatchId } : {}),
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to release contacts");
  }
  return res.json();
}

/* ── Bucket merge ──────────────────────────────────────────────────────── */

export interface MergeBlockingBucket {
  id: string;
  name: string;
  assignment_count: number;
}

export interface MergeBucketsResult {
  keeper_bucket_id: string;
  keeper_name: string;
  contacts_moved: number;
  merged_bucket_ids: string[];
  merged_bucket_count: number;
  keeper_total_contacts: number;
  keeper_remaining_contacts: number;
}

export class MergeBlockedError extends Error {
  blocking: MergeBlockingBucket[];
  constructor(message: string, blocking: MergeBlockingBucket[]) {
    super(message);
    this.blocking = blocking;
    this.name = "MergeBlockedError";
  }
}

export async function mergeBuckets(data: {
  keeper_bucket_id: string;
  source_bucket_ids: string[];
}): Promise<MergeBucketsResult> {
  const res = await fetch(`${API_URL}/outreach/buckets/merge`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (res.status === 409) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail ?? {};
    throw new MergeBlockedError(
      detail.message || "Merge blocked by existing assignments",
      Array.isArray(detail.blocking_buckets) ? detail.blocking_buckets : []
    );
  }
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to merge buckets"));
  return res.json();
}

export async function updateCopy(
  copyId: string,
  data: { text?: string; is_primary?: boolean }
): Promise<ApiCopy> {
  const res = await fetch(`${API_URL}/outreach/copies/${copyId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update copy");
  return res.json();
}

export async function createCopy(
  bucketId: string,
  data: { copy_type: "title" | "description"; text?: string }
): Promise<ApiCopy> {
  const res = await fetch(`${API_URL}/outreach/buckets/${bucketId}/copies`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create copy");
  return res.json();
}

export async function regenerateCopy(
  copyId: string,
  feedback: string
): Promise<ApiCopy> {
  const res = await fetch(`${API_URL}/outreach/copies/${copyId}/regenerate`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ feedback }),
  });
  if (!res.ok) throw new Error("Failed to regenerate copy");
  return res.json();
}

export async function deleteCopy(copyId: string): Promise<void> {
  const res = await fetch(`${API_URL}/outreach/copies/${copyId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete copy");
}

/* ── Outreach: Senders ─────────────────────────────────────────────────── */

export async function fetchSenders(): Promise<{ senders: ApiSender[] }> {
  const res = await fetch(`${API_URL}/outreach/senders`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch senders");
  return res.json();
}

export async function createSender(data: {
  name: string;
  total_accounts?: number;
  send_per_account?: number;
  days_per_webinar?: number;
  color?: string;
}): Promise<ApiSender> {
  const res = await fetch(`${API_URL}/outreach/senders`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create sender");
  return res.json();
}

export async function updateSender(
  senderId: string,
  data: Partial<{ name: string; total_accounts: number; send_per_account: number; days_per_webinar: number; color: string; is_active: boolean }>
): Promise<ApiSender> {
  const res = await fetch(`${API_URL}/outreach/senders/${senderId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update sender");
  return res.json();
}

/* ── Outreach: Webinars ────────────────────────────────────────────────── */

export async function fetchWebinars(): Promise<{ webinars: ApiWebinar[] }> {
  const res = await fetch(`${API_URL}/outreach/webinars`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch webinars");
  return res.json();
}

export async function createWebinar(data: {
  number: number;
  date: string;
  variant_label?: string | null;
  webinargeek_credential_id?: string | null;
}): Promise<ApiWebinar> {
  const res = await fetch(`${API_URL}/outreach/webinars`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to create webinar");
  }
  return res.json();
}

export async function updateWebinar(
  webinarId: string,
  data: Partial<{
    number: number;
    date: string;
    status: string;
    broadcast_id: string;
    main_title: string;
    registration_link: string;
    unsubscribe_link: string;
    variant_label: string | null;
    webinargeek_credential_id: string | null;
    nonjoiner_source_webinar_id: string | null;
  }>
): Promise<ApiWebinar> {
  const res = await fetch(`${API_URL}/outreach/webinars/${webinarId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to update webinar");
  }
  return res.json();
}

/* ── WebinarGeek connector credentials (multi-account) ────────────────── */

export interface ApiWgCredential {
  id: string;
  name: string;
  api_key_masked: string;
  created_at: string | null;
  updated_at: string | null;
}

export async function fetchWgCredentials(): Promise<{ credentials: ApiWgCredential[] }> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/credentials`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch WebinarGeek credentials");
  return res.json();
}

export async function createWgCredential(data: { name: string; api_key: string }): Promise<ApiWgCredential> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/credentials`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to create credential");
  }
  return res.json();
}

export async function updateWgCredential(
  credentialId: string,
  data: { name?: string; api_key?: string },
): Promise<ApiWgCredential> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/credentials/${credentialId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to update credential");
  }
  return res.json();
}

export async function deleteWgCredential(credentialId: string): Promise<{ deleted: boolean }> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/credentials/${credentialId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to delete credential");
  }
  return res.json();
}

export async function deleteWebinar(webinarId: string): Promise<{ deleted: boolean; released: number }> {
  const res = await fetch(`${API_URL}/outreach/webinars/${webinarId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete webinar");
  return res.json();
}

/* ── Outreach: Assignments ─────────────────────────────────────────────── */

export async function fetchWebinarLists(webinarId: string): Promise<{ assignments: ApiAssignment[] }> {
  const res = await fetch(`${API_URL}/outreach/webinars/${webinarId}/lists`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch assignments");
  return res.json();
}

export async function assignBucketToWebinar(
  webinarId: string,
  data: {
    bucket_id?: string;
    upload_id?: string;
    sender_id: string;
    volume: number;
    accounts_used?: number;
    send_per_account?: number;
    days?: number;
    countries_override?: string;
    emp_range_override?: string;
  }
): Promise<ApiAssignment> {
  const res = await fetch(`${API_URL}/outreach/webinars/${webinarId}/assign`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to assign");
  }
  return res.json();
}

export async function updateAssignment(
  assignmentId: string,
  data: Partial<{ title_copy_id: string; desc_copy_id: string; accounts_used: number; volume: number; remaining: number; list_url: string; list_name: string; gcal_invited: number; is_setup: boolean }>
): Promise<ApiAssignment> {
  const res = await fetch(`${API_URL}/outreach/assignments/${assignmentId}`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update assignment");
  return res.json();
}

export async function deleteAssignment(assignmentId: string): Promise<{ released: number; bucket_id: string | null; bucket_remaining: number | null }> {
  const res = await fetch(`${API_URL}/outreach/assignments/${assignmentId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete assignment");
  return res.json();
}

/* ── Outreach: Custom Lists ────────────────────────────────────────────── */

export interface ApiCustomList {
  id: string;
  name: string;
  total_contacts: number;
  available_contacts: number;
  created_at: string | null;
}

export async function fetchCustomLists(): Promise<{ lists: ApiCustomList[] }> {
  const res = await fetch(`${API_URL}/outreach/uploads/custom-lists`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch custom lists");
  return res.json();
}

export async function fetchCustomListCopies(uploadId: string): Promise<{ upload_id: string; titles: ApiCopy[]; descriptions: ApiCopy[] }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/copies`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch custom list copies");
  return res.json();
}

export async function createCustomListCopy(
  uploadId: string,
  data: { copy_type: "title" | "description"; text: string }
): Promise<ApiCopy> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/copies`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create custom list copy");
  return res.json();
}

export async function generateCustomListCopies(
  uploadId: string,
  data: { copy_type: "title" | "description" | "both"; variant_count?: number }
): Promise<{ upload_id: string; batch_id: string; titles: ApiCopy[]; descriptions: ApiCopy[] }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/copies/generate`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to generate custom list copies");
  return res.json();
}

/* ── Outreach: Uploads ─────────────────────────────────────────────────── */

export async function fetchUploads(): Promise<{ uploads: ApiUpload[] }> {
  const res = await fetch(`${API_URL}/outreach/uploads`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch uploads");
  return res.json();
}

/* ── Direct-to-Supabase Upload ────────────────────────────────────────── */

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
  const text = await res.text();
  if (!text) return fallback;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail;
    }
  } catch {
    /* non-JSON error body */
  }
  return text;
}

export async function requestSignedUploadUrl(filename: string, fileSize: number): Promise<{
  upload_id: string;
  signed_url: string;
  storage_path: string;
}> {
  const res = await fetch(`${API_URL}/outreach/uploads/presign`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ filename, file_size: fileSize }),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to get signed URL"));
  }
  return res.json();
}

export function uploadToStorage(
  signedUrl: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", signedUrl, true);
    xhr.setRequestHeader("Content-Type", "text/csv");
    xhr.setRequestHeader("x-upsert", "true");

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`Storage upload failed: ${xhr.status} ${xhr.statusText}`));
      }
    };

    xhr.onerror = () => reject(new Error("Storage upload network error"));
    xhr.ontimeout = () => reject(new Error("Storage upload timed out"));
    // Dynamic timeout: 3s per MB, minimum 10 minutes
    xhr.timeout = Math.max(600000, (file.size / (1024 * 1024)) * 3000);

    xhr.send(file);
  });
}

export async function confirmUpload(
  uploadId: string,
  fileSize: number,
): Promise<UploadFileResponse> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/confirm`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ file_size: fileSize }),
  });
  if (!res.ok) {
    throw new Error(await readErrorDetail(res, "Failed to confirm upload"));
  }
  return res.json();
}

export async function startImport(
  uploadId: string,
  fieldMappings: Record<string, string>,
  duplicateMode: string = "ignore",
  uploadMode: string = "bucket",
  customListName?: string,
): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/import`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({
      field_mappings: fieldMappings,
      duplicate_mode: duplicateMode,
      upload_mode: uploadMode,
      custom_list_name: customListName,
    }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to start import"));
  return res.json();
}

export async function fetchUploadStatus(uploadId: string): Promise<UploadStatusResponse> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/status`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch upload status");
  return res.json();
}

export async function fetchUploadHeaders(uploadId: string): Promise<UploadFileResponse> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/headers`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch upload headers");
  return res.json();
}

export async function deleteUpload(uploadId: string): Promise<{ id: string; deleted_contacts: number; message: string }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text);
  }
  return res.json();
}

export async function pauseImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/pause`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to pause import");
  return res.json();
}

export async function resumeImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/resume`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to resume import");
  return res.json();
}

export async function cancelImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/outreach/uploads/${uploadId}/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to cancel import");
  return res.json();
}

/* ── Outreach: Custom Fields ───────────────────────────────────────────── */

export interface ApiCustomField {
  id: string;
  field_name: string;
  field_type: string;
  display_order: number;
}

export async function fetchCustomFields(): Promise<{ fields: ApiCustomField[] }> {
  const res = await fetch(`${API_URL}/outreach/custom-fields`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch custom fields");
  return res.json();
}

export async function createCustomField(data: {
  field_name: string;
  field_type?: string;
}): Promise<ApiCustomField> {
  const res = await fetch(`${API_URL}/outreach/custom-fields`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create custom field");
  return res.json();
}


/* ── Brain Management ────────────────────────────────────────────────────── */

export interface ApiPrinciple {
  id: string;
  principle_text: string;
  knowledge_type: string;
  category: string | null;
  is_active: boolean;
  display_order: number | null;
  times_applied: number;
  created_at: string | null;
}

export interface ApiCaseStudyMetric {
  label: string;
  before: string;
  after: string;
}

export interface ApiCaseStudyStructured {
  headline?: string;
  quote?: string;
  metrics?: ApiCaseStudyMetric[];
  pain_points?: string[];
  outcomes?: string[];
  persona?: {
    role?: string;
    company_size?: string;
    target_market?: string;
  };
  industry_aliases?: string[];
}

export interface ApiCaseStudy {
  id: string;
  title: string;
  client_name: string | null;
  industry: string | null;
  tags: string[];
  content: string;
  is_active: boolean;
  source_url: string | null;
  structured: ApiCaseStudyStructured | null;
  created_at: string | null;
}

export interface ApiBrainContent {
  universal_brain: string;
  format_brain: string;
  format_brain_id: string | null;
}

// Principles
export async function fetchPrinciples(): Promise<ApiPrinciple[]> {
  const res = await fetch(`${API_URL}/outreach/brain/principles`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch principles");
  return res.json();
}

export async function createPrinciple(data: {
  principle_text: string;
  knowledge_type?: string;
  category?: string;
}): Promise<ApiPrinciple> {
  const res = await fetch(`${API_URL}/outreach/brain/principles`, {
    method: "POST", headers: jsonHeaders(), body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create principle");
  return res.json();
}

export async function updatePrinciple(id: string, data: {
  principle_text?: string;
  category?: string;
  is_active?: boolean;
}): Promise<ApiPrinciple> {
  const res = await fetch(`${API_URL}/outreach/brain/principles/${id}`, {
    method: "PUT", headers: jsonHeaders(), body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update principle");
  return res.json();
}

export async function deletePrinciple(id: string): Promise<void> {
  const res = await fetch(`${API_URL}/outreach/brain/principles/${id}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete principle");
}

// Case Studies
export async function fetchCaseStudies(): Promise<ApiCaseStudy[]> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch case studies");
  return res.json();
}

export async function createCaseStudy(data: {
  title: string;
  client_name?: string;
  industry?: string;
  tags?: string[];
  content: string;
}): Promise<ApiCaseStudy> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies`, {
    method: "POST", headers: jsonHeaders(), body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to create case study");
  return res.json();
}

export async function updateCaseStudy(id: string, data: {
  title?: string;
  client_name?: string;
  industry?: string;
  tags?: string[];
  content?: string;
  is_active?: boolean;
}): Promise<ApiCaseStudy> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies/${id}`, {
    method: "PUT", headers: jsonHeaders(), body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to update case study");
  return res.json();
}

export async function deleteCaseStudy(id: string): Promise<void> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies/${id}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete case study");
}

export async function importCaseStudyFromUrl(data: {
  url: string;
  notes?: string;
}): Promise<ApiCaseStudy> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies/import`, {
    method: "POST", headers: jsonHeaders(), body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to import case study"));
  return res.json();
}

export async function reextractCaseStudy(id: string): Promise<ApiCaseStudy> {
  const res = await fetch(`${API_URL}/outreach/brain/case-studies/${id}/reextract`, {
    method: "POST", headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to re-extract case study"));
  return res.json();
}

// Brain Content
export async function fetchBrainContent(): Promise<ApiBrainContent> {
  const res = await fetch(`${API_URL}/outreach/brain/content`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch brain content");
  return res.json();
}

export async function updateUniversalBrain(brain_content: string): Promise<{ brain_content: string; version: number }> {
  const res = await fetch(`${API_URL}/outreach/brain/content/universal`, {
    method: "PUT", headers: jsonHeaders(), body: JSON.stringify({ brain_content }),
  });
  if (!res.ok) throw new Error("Failed to update universal brain");
  return res.json();
}

export async function updateFormatBrain(brain_content: string): Promise<{ brain_content: string }> {
  const res = await fetch(`${API_URL}/outreach/brain/content/format`, {
    method: "PUT", headers: jsonHeaders(), body: JSON.stringify({ brain_content }),
  });
  if (!res.ok) throw new Error("Failed to update format brain");
  return res.json();
}


/* ── Assignment Contacts ──────────────────────────────────────────────────── */

export interface ApiContact {
  id: string;
  email: string;
  first_name: string | null;
  outreach_status: "assigned" | "used";
  used_at: string | null;
}

export interface AssignmentContactsResponse {
  assignment: {
    id: string;
    bucket_name: string | null;
    list_name: string | null;
    webinar_number: number | null;
    webinar_date: string | null;
    volume: number;
  };
  contacts: ApiContact[];
  counts: { assigned: number; used: number; total: number };
}

export async function fetchAssignmentContacts(
  assignmentId: string,
  status: "assigned" | "used" | "all" = "assigned"
): Promise<AssignmentContactsResponse> {
  const res = await fetch(`${API_URL}/outreach/assignments/${assignmentId}/contacts?status=${status}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch contacts");
  return res.json();
}

export async function markContactsUsed(
  assignmentId: string,
  contactIds: string[]
): Promise<{ marked: number }> {
  const res = await fetch(`${API_URL}/outreach/assignments/${assignmentId}/contacts/mark-used`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ contact_ids: contactIds }),
  });
  if (!res.ok) throw new Error("Failed to mark contacts as used");
  return res.json();
}

export interface AssignmentGroupContactsResponse {
  group: {
    assignment_ids: string[];
    bucket_name: string | null;
    webinar_number: number | null;
    webinar_date: string | null;
    list_count: number;
    volume: number;
    volume_raw: number;
    blocklisted_total: number;
  };
  contacts: ApiContact[];
  counts: { assigned: number; used: number; total: number };
  pagination: { limit: number; offset: number; returned: number; filtered_total: number };
}

export async function fetchAssignmentGroupContacts(
  assignmentIds: string[],
  status: "assigned" | "used" | "all" = "assigned",
  opts: { limit?: number; offset?: number } = {},
): Promise<AssignmentGroupContactsResponse> {
  const ids = assignmentIds.join(",");
  const params = new URLSearchParams({ ids, status });
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));
  const res = await fetch(`${API_URL}/outreach/assignment-groups/contacts?${params.toString()}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch group contacts");
  return res.json();
}

export async function downloadAssignmentGroupContactsCsv(
  assignmentIds: string[],
  status: "assigned" | "used" | "all" = "all",
): Promise<{ blob: Blob; filename: string }> {
  const ids = assignmentIds.join(",");
  const res = await fetch(`${API_URL}/outreach/assignment-groups/contacts.csv?ids=${encodeURIComponent(ids)}&status=${status}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to export CSV");
  const blob = await res.blob();
  // Server sets Content-Disposition; pull a filename from it when present.
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const filename = match ? match[1] : `group_contacts_${status}.csv`;
  return { blob, filename };
}

export async function markGroupContactsUsed(
  contactIds: string[]
): Promise<{ marked: number; by_assignment: Record<string, number> }> {
  const res = await fetch(`${API_URL}/outreach/assignment-groups/contacts/mark-used`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ contact_ids: contactIds }),
  });
  if (!res.ok) throw new Error("Failed to mark group contacts as used");
  return res.json();
}

/* ── Statistics ─────────────────────────────────────────────────────────── */

export interface StatisticsMetrics {
  // Raw source fields
  gcalInvitedGhl: number | null;
  accountsNeeded: number | null;
  invited: number | null;
  actuallyUsed: number | null;
  unsubscribes: number | null;
  lpRegs: number | null;
  yesMarked: number | null;
  yesAttended: number | null;
  yes10MinPlus: number | null;
  yesAttendBySmsClick: number | null;
  yesBookings: number | null;
  maybeMarked: number | null;
  maybeAttended: number | null;
  maybe10MinPlus: number | null;
  maybeAttendBySmsClick: number | null;
  maybeBookings: number | null;
  selfRegMarked: number | null;
  selfRegAttended: number | null;
  selfReg10MinPlus: number | null;
  selfRegBookings: number | null;
  totalRegs: number | null;
  totalAttended: number | null;
  attendBySmsReminder: number | null;
  total10MinPlus: number | null;
  total30MinPlus: number | null;
  totalBookings: number | null;
  totalCallsDatePassed: number | null;
  confirmed: number | null;
  shows: number | null;
  noShows: number | null;
  canceled: number | null;
  won: number | null;
  disqualified: number | null;
  qualified: number | null;
  leadQualityGreat: number | null;
  leadQualityOk: number | null;
  leadQualityBarelyPassable: number | null;
  leadQualityBadDq: number | null;
  avgProjectedDealSize: number | null;
  avgClosedDealValue: number | null;
  // Derived fields
  unsubPercent: number | null;
  yesPer1kInv: number | null;
  yesPercent: number | null;
  yesAttendPercent: number | null;
  yesStay10MinPercent: number | null;
  yesAttendBySmsClickPercent: number | null;
  yesBookingsPer1kInv: number | null;
  maybePer1kInv: number | null;
  maybeAttendPercent: number | null;
  maybeStay10MinPercent: number | null;
  maybeAttendBySmsClickPercent: number | null;
  maybeBookingsPer1kInv: number | null;
  selfRegPer1kInv: number | null;
  selfRegAttendPercent: number | null;
  selfRegStay10MinPercent: number | null;
  selfRegBookingsPer1kInv: number | null;
  invitedToRegPercent: number | null;
  regToAttendPercent: number | null;
  invitedToAttendPercent: number | null;
  totalAttendedPer1kInv: number | null;
  attendBySmsReminderPercent: number | null;
  total10MinPlusPer1kInv: number | null;
  attend10MinPercent: number | null;
  total30MinPlusPer1kInv: number | null;
  attend30MinPercent: number | null;
  bookingsPerAttended: number | null;
  bookingsPerPast10Min: number | null;
  totalBookingsPer1kInv: number | null;
  showPercent: number | null;
  closeRatePercent: number | null;
  qualPercent: number | null;
  [key: string]: number | null;
}

export interface StatisticsCopy {
  id: string;
  text: string;
  variantIndex: number;
}

export interface ApiStatisticsRow {
  id: string;
  webinarNumber: number;
  workbookRow: number;
  assignmentId: string | null;
  kind: "list" | "nonjoiners" | "no_list_data";
  status: string | null;
  note: string | null;
  listUrl: string | null;
  description: string | null;
  listName: string | null;
  sendInfo: string | null;
  senderColor: string | null;
  bucketId: string | null;
  bucketName: string | null;
  descLabel: string | null;
  titleText: string | null;
  titleCopy: StatisticsCopy | null;
  descCopy: StatisticsCopy | null;
  segmentName: string | null;
  createdDate: string | null;
  industry: string | null;
  employeeRange: string | null;
  country: string | null;
  metrics: StatisticsMetrics;
  /** True when rate metrics fell back to `invited` because `actuallyUsed`
   * was null/0 — surfaces a tag on the row so operators know the
   * denominator is the planned number, not the live sent number. */
  usedFallback: boolean;
  /** Set on the synthetic NO LIST DATA row only, when sibling A/B variants
   * exist for this webinar's number. The Yes/Maybe/booked-call portion of
   * NO LIST DATA can't be split between variants (GHL stores only N), so
   * the same numbers appear on both variants' NO LIST DATA rows. The UI
   * renders a "shared signals" tag when this is true. */
  sharedAcrossVariants?: boolean;
}

export interface ApiStatisticsWebinar {
  id: string;
  /** Underlying Webinar.id (UUID). Use this for drill-down requests and
   * client-side keying so A/B variants don't collide. */
  webinarId: string | null;
  number: number;
  /** Free-text variant tag — null for non-variant webinars. */
  variantLabel: string | null;
  date: string | null;
  title: string | null;
  workbookRow: number;
  source: string;
  summary: StatisticsMetrics;
  /** Same fallback semantics as ApiStatisticsRow.usedFallback — applies
   * to the parent summary's rate metrics. */
  usedFallback: boolean;
  /** True when another webinar shares this `number` (A/B variant). The
   * UI renders a small variant pill and uses this to scope state by id. */
  hasSiblingVariants: boolean;
  rows: ApiStatisticsRow[];
}

export interface StatisticsMeta {
  source: "ghl" | "workbook";
  last_sync: {
    run_id: string;
    sync_type: string;
    status: string;
    started_at: string | null;
    completed_at: string | null;
    contacts_synced: number;
    opportunities_synced: number;
  } | null;
}

export async function fetchStatisticsWebinars(source: "auto" | "ghl" | "workbook" = "auto"): Promise<{ webinars: ApiStatisticsWebinar[]; meta: StatisticsMeta }> {
  const res = await fetch(`${API_URL}/statistics/webinars?source=${source}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch statistics");
  return res.json();
}

export interface ApiStatisticsWebinarSummary {
  /** Synthetic id like "stat-w136" or "stat-w136-Account A". Stable per
   * variant — used as a React key. */
  id: string;
  /** Underlying Webinar.id (UUID). Pass to fetchStatisticsWebinar() and
   * fetchStatisticsContacts() to disambiguate variants. */
  webinarId: string | null;
  number: number;
  /** Free-text variant tag, e.g. "Account A". null for the unique row of
   * a non-variant number. */
  variantLabel: string | null;
  date: string | null;
  title: string | null;
  status: string | null;
  listCount: number;
  broadcastId: string | null;
}

export async function fetchStatisticsWebinarList(
  source: "auto" | "ghl" | "workbook" = "auto",
): Promise<{ webinars: ApiStatisticsWebinarSummary[]; meta: StatisticsMeta }> {
  const res = await fetch(`${API_URL}/statistics/webinars/list?source=${source}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch statistics list");
  return res.json();
}

export async function fetchStatisticsWebinar(
  webinarId: string,
  source: "auto" | "ghl" | "workbook" = "auto",
): Promise<ApiStatisticsWebinar> {
  const res = await fetch(`${API_URL}/statistics/webinars/${webinarId}?source=${source}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`Failed to fetch webinar ${webinarId}`);
  return res.json();
}

export interface ContactDrilldownItem {
  ghl_contact_id: string;
  email: string | null;
  first_name: string | null;
  last_name: string | null;
  company_website: string | null;
  assignment_id: string | null;
  ghl_url: string;
  // Booking-source UTMs (GHL contact "Book - Campaign *" fields)
  book_source?: string | null;
  book_medium?: string | null;
  book_name?: string | null;
  book_content?: string | null;
  book_term?: string | null;
  book_id?: string | null;
  opportunity_id?: string | null;
  opportunity_url?: string | null;
  opportunity_stage_id?: string | null;
  opportunity_value?: number | null;
  owner?: string | null;
  call1_status?: string | null;
  call1_date?: string | null;
  call1_booking_date?: string | null;
  webinar_source_number?: number | null;
  lead_quality?: string | null;
}

export interface ContactDrilldownResponse {
  metric: string;
  webinar_number: number;
  webinar_id: string | null;
  assignment_id: string | null;
  unit: "contact" | "opportunity";
  total: number;
  items: ContactDrilldownItem[];
  available: boolean;
  reason: string | null;
}

export async function fetchStatisticsContacts(params: {
  /** Either webinarId (preferred — disambiguates A/B variants) or
   * webinarNumber (back-compat; resolves to the unlabeled variant). */
  webinarId?: string | null;
  webinarNumber?: number | null;
  metric: string;
  assignment?: string | null;
  limit?: number;
}): Promise<ContactDrilldownResponse> {
  const qs = new URLSearchParams({ metric: params.metric });
  if (params.webinarId) qs.set("webinar_id", params.webinarId);
  else if (params.webinarNumber != null) qs.set("webinar", String(params.webinarNumber));
  if (params.assignment) qs.set("assignment", params.assignment);
  if (params.limit) qs.set("limit", String(params.limit));
  const res = await fetch(`${API_URL}/statistics/contacts?${qs.toString()}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch contacts drill-down");
  return res.json();
}

/* ── List-name distribution ─────────────────────────────────────────────── */

export interface ListDistributionItem {
  list_name: string | null;
  count: number;
  pct: number; // 0–100
}

export interface DomainItem {
  domain: string | null;
  count: number;
  pct: number; // 0–100
  is_free: boolean;
}

export interface DomainDistribution {
  total: number; // contacts with a parseable email (the % denominator)
  unique_domains: number;
  free_domain_contacts: number;
  free_domain_unique: number;
  top: DomainItem[];
  free: DomainItem[];
}

export interface ListDistributionResponse {
  scope: "assignment" | "webinar";
  assignment_id: string | null;
  webinar_id: string | null;
  webinar_number: number | null;
  label: string | null;
  total: number;
  items: ListDistributionItem[];
  domains: DomainDistribution;
}

/** Distribution of source list names (contacts.lead_list_name) for either a
 * single assigned list (`assignment`) or all assigned lists on a webinar
 * (`webinarId` / `webinarNumber`). */
export async function fetchListDistribution(params: {
  assignment?: string | null;
  webinarId?: string | null;
  webinarNumber?: number | null;
}): Promise<ListDistributionResponse> {
  const qs = new URLSearchParams();
  if (params.assignment) qs.set("assignment", params.assignment);
  else if (params.webinarId) qs.set("webinar_id", params.webinarId);
  else if (params.webinarNumber != null) qs.set("webinar", String(params.webinarNumber));
  const res = await fetch(`${API_URL}/statistics/list-distribution?${qs.toString()}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch list distribution");
  return res.json();
}

/* ── GHL Sync ───────────────────────────────────────────────────────────── */

export interface GhlSyncRun {
  id: string;
  sync_type: string; // "full" | "incremental" | "webinar:N:narrow" | "webinar:N:deep"
  trigger: "scheduled" | "manual";
  status: "running" | "completed" | "failed" | "cancelled";
  started_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  contacts_synced: number;
  opportunities_synced: number;
  expected_total: number | null;
  errors_count: number;
  error_details: unknown[] | null;
  cancel_requested: boolean;
  last_heartbeat_at: string | null;
}

export interface GhlSyncStatus {
  latest: GhlSyncRun | null;
  is_running: boolean;
}

export interface GhlSyncSettings {
  incremental_enabled: boolean;
  incremental_interval_hours: number;
  weekly_full_enabled: boolean;
  weekly_full_day_of_week: string;
  weekly_full_hour_local: number;
  weekly_full_timezone: string;
  updated_at: string | null;
}

export async function fetchGhlSyncStatus(): Promise<GhlSyncStatus> {
  const res = await fetch(`${API_URL}/ghl-sync/status`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch sync status");
  return res.json();
}

export async function fetchGhlSyncHistory(limit = 50): Promise<{ runs: GhlSyncRun[] }> {
  const res = await fetch(`${API_URL}/ghl-sync/history?limit=${limit}`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch sync history");
  return res.json();
}

export async function triggerGhlSync(syncType: "full" | "incremental"): Promise<{ run_id: string; sync_type: string; status: string }> {
  const res = await fetch(`${API_URL}/ghl-sync/trigger?sync_type=${syncType}`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to trigger sync");
  }
  return res.json();
}

export async function triggerGhlWebinarSync(
  webinarNumber: number,
  phase: "narrow" | "deep" | "full" = "full",
): Promise<{ run_id: string; sync_type: string; status: string }> {
  const res = await fetch(
    `${API_URL}/ghl-sync/trigger-webinar?n=${webinarNumber}&phase=${phase}`,
    { method: "POST", headers: authHeaders() },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to trigger webinar sync");
  }
  return res.json();
}

export async function cancelGhlSyncRun(runId: string): Promise<GhlSyncRun> {
  const res = await fetch(`${API_URL}/ghl-sync/runs/${runId}/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to cancel sync");
  }
  return res.json();
}

export async function recoverStaleGhlSyncs(): Promise<{ recovered: number; swept: number }> {
  const res = await fetch(`${API_URL}/ghl-sync/admin/recover-stale`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to recover stale syncs");
  }
  return res.json();
}

export async function fetchGhlSyncSettings(): Promise<GhlSyncSettings> {
  const res = await fetch(`${API_URL}/ghl-sync/settings`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch sync settings");
  return res.json();
}

export async function updateGhlSyncSettings(payload: Partial<GhlSyncSettings>): Promise<GhlSyncSettings> {
  const res = await fetch(`${API_URL}/ghl-sync/settings`, {
    method: "PATCH",
    headers: jsonHeaders(),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to update settings");
  }
  return res.json();
}

/* ── Connectors: WebinarGeek ───────────────────────────────────────────── */

export interface WgCredentialStatus {
  configured: boolean;
  api_key_masked?: string | null;
}

export interface WgWebinar {
  broadcast_id: string;
  name: string;
  internal_title: string | null;
  starts_at: string | null;
  duration_seconds: number | null;
  subscriptions_count: number;
  live_viewers_count: number;
  replay_viewers_count: number;
  has_ended: boolean;
  cancelled: boolean;
  last_synced_at: string | null;
  synced_subscriber_count: number;
  credential_id: string | null;
  credential_name: string | null;
}

export interface WgSubscriber {
  id: string;
  broadcast_id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  registration_source: string | null;
  subscribed_at: string | null;
  watched_live: boolean | null;
  watched_replay: boolean | null;
  minutes_viewing: number | null;
  viewing_device: string | null;
  viewing_country: string | null;
}

export async function fetchWgStatus(): Promise<WgCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/webinargeek`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch WebinarGeek status");
  return res.json();
}

export async function saveWgApiKey(api_key: string): Promise<WgCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/webinargeek`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ api_key }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to save API key"));
  return res.json();
}

export async function deleteWgApiKey(): Promise<void> {
  const res = await fetch(`${API_URL}/connectors/webinargeek`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete API key");
}

/* ── Connectors: OpenAI ────────────────────────────────────────────────── */

export interface OpenAiCredentialStatus {
  configured: boolean;
  api_key_masked?: string | null;
}

export async function fetchOpenAiStatus(): Promise<OpenAiCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/openai`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch OpenAI status");
  return res.json();
}

export async function saveOpenAiApiKey(api_key: string): Promise<OpenAiCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/openai`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ api_key }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to save OpenAI API key"));
  return res.json();
}

export async function deleteOpenAiApiKey(): Promise<void> {
  const res = await fetch(`${API_URL}/connectors/openai`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete OpenAI API key");
}

/* ── Connectors: Anthropic (Claude chat) ───────────────────────────────── */

export interface AnthropicCredentialStatus {
  configured: boolean;
  api_key_masked?: string | null;
}

export async function fetchAnthropicStatus(): Promise<AnthropicCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/anthropic`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch Anthropic status");
  return res.json();
}

export async function saveAnthropicApiKey(api_key: string): Promise<AnthropicCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/anthropic`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ api_key }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to save Anthropic API key"));
  return res.json();
}

export async function deleteAnthropicApiKey(): Promise<void> {
  const res = await fetch(`${API_URL}/connectors/anthropic`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete Anthropic API key");
}

/* ── Connectors: GoHighLevel ───────────────────────────────────────────── */

export interface GhlCredentialStatus {
  configured: boolean;
  api_key_masked?: string | null;
  location_id?: string | null;
  pipeline_id?: string | null;
  source: "db" | "env" | "none";
}

export async function fetchGhlConnectorStatus(): Promise<GhlCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/ghl`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch GHL status");
  return res.json();
}

export async function saveGhlConnector(
  api_key: string,
  location_id: string,
  pipeline_id?: string | null,
): Promise<GhlCredentialStatus> {
  const res = await fetch(`${API_URL}/connectors/ghl`, {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ api_key, location_id, pipeline_id: pipeline_id || null }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to save GHL credentials"));
  return res.json();
}

export async function deleteGhlConnector(): Promise<void> {
  const res = await fetch(`${API_URL}/connectors/ghl`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to delete GHL credentials");
}

export async function fetchWgWebinars(opts?: { limit?: number; offset?: number; q?: string; credential_id?: string }): Promise<{ broadcasts: WgWebinar[]; total: number }> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.offset != null) params.set("offset", String(opts.offset));
  if (opts?.q) params.set("q", opts.q);
  if (opts?.credential_id) params.set("credential_id", opts.credential_id);
  const res = await fetch(
    `${API_URL}/connectors/webinargeek/webinars${params.toString() ? `?${params}` : ""}`,
    { headers: authHeaders() }
  );
  if (!res.ok) throw new Error("Failed to fetch broadcasts");
  return res.json();
}

export async function refreshWgWebinars(): Promise<{ count: number }> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/webinars/refresh`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to refresh broadcasts"));
  return res.json();
}

export async function syncWgSubscribers(broadcastId: string): Promise<{
  broadcast_id: string;
  run_id: string;
  status: string;
}> {
  const res = await fetch(
    `${API_URL}/connectors/webinargeek/webinars/${encodeURIComponent(broadcastId)}/sync`,
    { method: "POST", headers: authHeaders() }
  );
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to start subscriber sync"));
  return res.json();
}

export async function syncAllWgSubscribers(): Promise<{
  run_id: string;
  status: string;
  broadcasts_queued: number;
}> {
  const res = await fetch(`${API_URL}/connectors/webinargeek/webinars/sync-all`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to start sync-all"));
  return res.json();
}

export async function fetchWgSubscribers(opts: {
  broadcast_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}): Promise<{ subscribers: WgSubscriber[]; total: number }> {
  const params = new URLSearchParams();
  if (opts.broadcast_id) params.set("broadcast_id", opts.broadcast_id);
  if (opts.q) params.set("q", opts.q);
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));
  const res = await fetch(
    `${API_URL}/connectors/webinargeek/subscribers?${params}`,
    { headers: authHeaders() }
  );
  if (!res.ok) throw new Error("Failed to fetch subscribers");
  return res.json();
}

export function wgSubscribersCsvUrl(opts: { broadcast_id?: string; q?: string }): string {
  const params = new URLSearchParams();
  if (opts.broadcast_id) params.set("broadcast_id", opts.broadcast_id);
  if (opts.q) params.set("q", opts.q);
  return `${API_URL}/connectors/webinargeek/subscribers/export?${params}`;
}

/* ── Blocklist ─────────────────────────────────────────────────────────── */

export type BlocklistSource = "ghl_dnd" | "wg_unsub" | "manual" | "csv";

export interface BlocklistEntry {
  id: string;
  email: string;
  source: BlocklistSource;
  reason: string | null;
  source_ref: string | null;
  created_at: string | null;
}

export interface BlocklistListResponse {
  entries: BlocklistEntry[];
  total: number;
  by_source: Partial<Record<BlocklistSource, number>>;
}

export async function fetchBlocklist(opts: {
  q?: string;
  source?: BlocklistSource;
  limit?: number;
  offset?: number;
} = {}): Promise<BlocklistListResponse> {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.source) params.set("source", opts.source);
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));
  const qs = params.toString();
  const res = await fetch(`${API_URL}/blocklist${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch blocklist");
  return res.json();
}

export async function addBlocklistEntry(data: {
  email: string;
  reason?: string;
}): Promise<BlocklistEntry> {
  const res = await fetch(`${API_URL}/blocklist`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to add entry");
  }
  return res.json();
}

export async function bulkAddBlocklist(data: {
  emails: string[];
  reason?: string;
}): Promise<{ added: number; skipped: number; invalid: number }> {
  const res = await fetch(`${API_URL}/blocklist/bulk`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to import emails");
  }
  return res.json();
}

export async function deleteBlocklistEntry(entryId: string): Promise<void> {
  const res = await fetch(`${API_URL}/blocklist/${entryId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to remove entry");
  }
}

export interface BlocklistBackfillResult {
  wg_scanned: number;
  wg_added: number;
  ghl_scanned: number;
  ghl_added: number;
}

export async function backfillBlocklist(): Promise<BlocklistBackfillResult> {
  const res = await fetch(`${API_URL}/blocklist/backfill`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail ?? "Failed to backfill blocklist");
  }
  return res.json();
}

/* ── Calendar Uploads (per-webinar Added-to-Calendar CSVs) ─────────────── */

export interface ApiCalendarUpload {
  id: string;
  webinar_id: string;
  webinar_label: string | null;
  sender_id: string | null;
  sender_name: string | null;
  file_name: string;
  status: string;
  progress: number;
  has_responses: boolean;
  total_rows: number;
  processed_rows: number;
  matched_count: number;
  unmatched_count: number;
  error_message: string | null;
  created_at: string | null;
  completed_at: string | null;
}

export interface CalendarConfirmResponse {
  id: string;
  file_name: string;
  webinar_id: string;
  total_rows: number;
  has_responses: boolean;
  headers: string[];
  preview_rows: string[][];
}

export async function fetchCalendarUploads(): Promise<{ uploads: ApiCalendarUpload[] }> {
  const res = await fetch(`${API_URL}/calendar-uploads`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch calendar uploads");
  return res.json();
}

export async function presignCalendarUpload(
  filename: string,
  fileSize: number,
  webinarId: string,
  senderId?: string | null,
): Promise<{ upload_id: string; signed_url: string; storage_path: string }> {
  const res = await fetch(`${API_URL}/calendar-uploads/presign`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({
      filename,
      file_size: fileSize,
      webinar_id: webinarId,
      sender_id: senderId ?? null,
    }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to get signed URL"));
  return res.json();
}

export async function confirmCalendarUpload(
  uploadId: string,
  fileSize: number,
): Promise<CalendarConfirmResponse> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/confirm`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ file_size: fileSize }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to confirm calendar upload"));
  return res.json();
}

export async function startCalendarImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/import`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to start calendar import"));
  return res.json();
}

export async function fetchCalendarUploadStatus(uploadId: string): Promise<ApiCalendarUpload> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/status`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch calendar upload status");
  return res.json();
}

export async function pauseCalendarImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/pause`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to pause calendar import");
  return res.json();
}

export async function resumeCalendarImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/resume`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to resume calendar import");
  return res.json();
}

export async function cancelCalendarImport(uploadId: string): Promise<{ id: string; status: string }> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to cancel calendar import");
  return res.json();
}

export async function deleteCalendarUpload(uploadId: string): Promise<{ id: string; deleted: boolean }> {
  const res = await fetch(`${API_URL}/calendar-uploads/${uploadId}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to delete calendar upload"));
  return res.json();
}

export interface ApiAccountHealthWebinar {
  id: string;
  number: number;
  variant_label: string | null;
  label: string;
  has_upload: boolean;
}

export interface ApiAccountHealthCell {
  total_sent: number;
  yes: number;
  maybe: number;
}

export interface ApiAccountHealthRow {
  calendar_account: string;
  per_webinar: Record<string, ApiAccountHealthCell>;
}

export interface ApiAccountHealthSender {
  id: string;
  name: string;
  color: string | null;
}

export interface CalendarAccountHealthResponse {
  webinars: ApiAccountHealthWebinar[];
  accounts: ApiAccountHealthRow[];
  totals: Record<string, ApiAccountHealthCell>;
  senders: ApiAccountHealthSender[];
  /** sender_map[webinar_id][calendar_account] = sender_id */
  sender_map: Record<string, Record<string, string>>;
  /** sender_names[sender_id] = name */
  sender_names: Record<string, string>;
}

export async function fetchCalendarAccountHealth(): Promise<CalendarAccountHealthResponse> {
  const res = await fetch(`${API_URL}/calendar-uploads/account-health`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to fetch account health"));
  return res.json();
}

export async function setCalendarAccountSendersBulk(
  webinarId: string,
  senderId: string,
  accounts: string[],
): Promise<{ webinar_id: string; sender_id: string; saved: number }> {
  const res = await fetch(`${API_URL}/calendar-uploads/account-senders/bulk`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ webinar_id: webinarId, sender_id: senderId, accounts }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to save account senders"));
  return res.json();
}

export interface ApiDayOfWeekCell {
  webinar_id: string;
  calendar_account: string;
  /** Postgres EXTRACT(DOW): 0=Sunday … 6=Saturday */
  dow: number;
  sent: number;
  yes: number;
  maybe: number;
}

export interface ApiDayOfWeekSkipped {
  webinar_id: string;
  calendar_account: string;
  /** Rows with NULL calendar_invited_date — cannot be bucketed by weekday. */
  count: number;
}

export interface CalendarDayOfWeekResponse {
  webinars: ApiAccountHealthWebinar[];
  cells: ApiDayOfWeekCell[];
  skipped: ApiDayOfWeekSkipped[];
  senders: ApiAccountHealthSender[];
  sender_map: Record<string, Record<string, string>>;
  sender_names: Record<string, string>;
}

export async function fetchCalendarDayOfWeek(): Promise<CalendarDayOfWeekResponse> {
  const res = await fetch(`${API_URL}/calendar-uploads/day-of-week`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res, "Failed to fetch day-of-week stats"));
  return res.json();
}
