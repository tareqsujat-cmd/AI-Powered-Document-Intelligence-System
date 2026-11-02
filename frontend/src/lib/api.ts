export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
const API = `${API_URL}/api/v1`;

export type DocumentStatus = "queued" | "processing" | "completed" | "failed";
export type FileType = "pdf" | "jpeg" | "png" | "tiff" | "xlsx" | "xls" | "csv";
export type Severity = "error" | "warning" | "info";
export type SearchMode = "keyword" | "semantic" | "hybrid";
export type FieldName =
  | "invoice_number"
  | "invoice_date"
  | "supplier_name"
  | "customer_name"
  | "subtotal"
  | "tax"
  | "total"
  | "currency";

export interface DocumentSummary {
  id: string;
  filename: string;
  file_type: FileType;
  size_bytes: number;
  page_count: number | null;
  status: DocumentStatus;
  stage: string;
  progress: number;
  needs_review: boolean;
  failure_reason: string | null;
  is_retryable: boolean;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  processing_started_at: string | null;
  processing_finished_at: string | null;
  invoice_count: number;
}

export interface DocumentDetail extends DocumentSummary {
  stored_page_count: number;
  validation_error_count: number;
}

export interface DocumentList {
  items: DocumentSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface DocumentFilters {
  status?: string;
  needs_review?: string;
  file_type?: string;
  filename?: string;
  supplier?: string;
  invoice_number?: string;
  date_from?: string;
  date_to?: string;
}

export interface ExtractedField {
  value: string | null;
  confidence: number;
  reason: string | null;
  source_snippet: string | null;
  source_page: number | null;
}

export interface LineItem {
  id: string;
  position: number;
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string | null;
  confidence: number;
  source_snippet: string | null;
  source_page: number | null;
}

export interface ValidationIssue {
  id: string;
  code: string;
  severity: Severity;
  field: string | null;
  message: string;
  expected_value: string | null;
  actual_value: string | null;
  related_document_id: string | null;
  is_resolved: boolean;
}

export interface InvoiceResult {
  id: string;
  invoice_index: number;
  version: number;
  page_start: number | null;
  page_end: number | null;
  method: "llm" | "rule_based";
  overall_confidence: number | null;
  warnings: string[];
  llm_model: string | null;
  prompt_version: string | null;
  fields: Record<FieldName, ExtractedField>;
  line_items: LineItem[];
  validation_errors: ValidationIssue[];
}

export interface PageContent {
  page_number: number;
  sheet_name: string | null;
  source: string | null;
  status: string;
  ocr_confidence: number | null;
  processing_ms: number | null;
  error: string | null;
  warnings: string[];
  text: string;
}

export interface HistoryEntry {
  id: number;
  event: string;
  stage: string | null;
  attempt: number | null;
  duration_ms: number | null;
  message: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  actor: string;
  created_at: string;
}

export interface Metrics {
  documents_total: number;
  documents_by_status: Record<string, number>;
  needs_review: number;
  success_rate: number | null;
  processing_time: { mean_seconds: number | null; p95_seconds: number | null };
}

export interface SearchHit {
  document_id: string;
  filename: string;
  page_number: number | null;
  snippet: string;
  score: number;
  matched_by: string[];
}

export interface UploadOutcome {
  filename: string;
  accepted: boolean;
  document: DocumentSummary | null;
  error: { code: string; reason: string } | null;
}

export interface LineItemCorrection {
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string | null;
}

export type Correction = Partial<Record<FieldName, string | null>> & {
  line_items?: LineItemCorrection[];
  actor?: string;
};

/** A failed request, with a message that is safe and useful to show to a person. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function messageFrom(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === "string") return first.msg;
  }
  if (status === 429) return "Too many requests. Try again shortly.";
  if (status >= 500) return "The server had a problem. Try again shortly.";
  return "The request could not be completed.";
}

function parseRetryAfter(value: string | null): number | null {
  const seconds = value === null ? NaN : Number(value);
  return Number.isFinite(seconds) ? seconds : null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, { cache: "no-store", ...init });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("The API could not be reached. Is the backend running?", 0);
  }
  if (response.ok) {
    try {
      return (await response.json()) as T;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError("The server sent an answer that could not be read.", response.status);
    }
  }
  const body: unknown = await response.json().catch(() => null);
  throw new ApiError(
    messageFrom(response.status, body),
    response.status,
    parseRetryAfter(response.headers.get("Retry-After")),
  );
}

function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function listDocuments(
  filters: DocumentFilters,
  limit: number,
  offset: number,
  signal?: AbortSignal,
): Promise<DocumentList> {
  return request(`/documents${queryString({ ...filters, limit, offset })}`, { signal });
}

export function getMetrics(signal?: AbortSignal): Promise<Metrics> {
  return request("/metrics", { signal });
}

export function getDocument(id: string, signal?: AbortSignal): Promise<DocumentDetail> {
  return request(`/documents/${id}`, { signal });
}

export function getResults(id: string, signal?: AbortSignal): Promise<InvoiceResult[]> {
  return request(`/documents/${id}/results`, { signal });
}

export function getPage(id: string, page: number, signal?: AbortSignal): Promise<PageContent> {
  return request(`/documents/${id}/pages/${page}`, { signal });
}

export function getHistory(id: string, signal?: AbortSignal): Promise<HistoryEntry[]> {
  return request(`/documents/${id}/history`, { signal });
}

export function correctResult(
  documentId: string,
  resultId: string,
  correction: Correction,
): Promise<InvoiceResult> {
  return sendJson(`/documents/${documentId}/results/${resultId}`, "PATCH", correction);
}

export function reviewIssue(
  documentId: string,
  issueId: string,
  isResolved: boolean,
): Promise<{ validation_error: ValidationIssue; needs_review: boolean }> {
  return sendJson(`/documents/${documentId}/validation-errors/${issueId}`, "PATCH", {
    is_resolved: isResolved,
  });
}

export function reprocessDocument(id: string, resume: boolean): Promise<unknown> {
  return sendJson(`/documents/${id}/reprocess${queryString({ resume: String(resume) })}`, "POST");
}

export function reprocessFailed(): Promise<{ queued: number; failed_to_queue: number }> {
  return sendJson("/documents/reprocess-failed", "POST");
}

export function search(
  query: string,
  mode: SearchMode,
  signal?: AbortSignal,
): Promise<{ results: SearchHit[] }> {
  return request(`/search${queryString({ q: query, mode, limit: 50 })}`, { signal });
}

export function pageImageUrl(id: string, page: number): string {
  return `${API}/documents/${id}/pages/${page}/image`;
}

export function fileUrl(id: string): string {
  return `${API}/documents/${id}/file`;
}

/** XMLHttpRequest, because fetch cannot report upload progress. */
export function uploadFile(
  file: File,
  onProgress: (percent: number) => void,
): Promise<UploadOutcome> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/documents`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () =>
      reject(new ApiError("The API could not be reached. Is the backend running?", 0));
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Handled below: an error with no usable body still gets a message.
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        const outcome = (body as { results?: UploadOutcome[] } | null)?.results?.[0];
        if (outcome) {
          resolve(outcome);
          return;
        }
      }
      reject(
        new ApiError(
          messageFrom(xhr.status, body),
          xhr.status,
          parseRetryAfter(xhr.getResponseHeader("Retry-After")),
        ),
      );
    };
    const form = new FormData();
    form.append("files", file);
    xhr.send(form);
  });
}
