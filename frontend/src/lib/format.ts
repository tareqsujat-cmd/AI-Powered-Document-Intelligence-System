import type { DocumentStatus, FieldName, InvoiceResult, Severity } from "./api";

export const FIELD_ORDER: FieldName[] = [
  "invoice_number",
  "invoice_date",
  "supplier_name",
  "customer_name",
  "subtotal",
  "tax",
  "total",
  "currency",
];

export const FIELD_LABELS: Record<FieldName, string> = {
  invoice_number: "Invoice number",
  invoice_date: "Invoice date",
  supplier_name: "Supplier",
  customer_name: "Customer",
  subtotal: "Subtotal",
  tax: "Tax",
  total: "Total",
  currency: "Currency",
};

export const MONEY_FIELDS: ReadonlySet<FieldName> = new Set(["subtotal", "tax", "total"]);

const STAGE_LABELS: Record<string, string> = {
  upload: "Waiting",
  file_validation: "Checking the file",
  text_extraction: "Reading text",
  llm_extraction: "Extracting invoice data",
  validation: "Validating",
  indexing: "Indexing for search",
  finished: "Finished",
};

const EVENT_LABELS: Record<string, string> = {
  uploaded: "Uploaded",
  stage_started: "Started",
  stage_completed: "Finished",
  stage_failed: "Failed",
  retry_scheduled: "Retry scheduled",
  reprocess_requested: "Reprocess requested",
  manual_correction: "Reviewed by a person",
};

/** Largest first, as in the API's own severity order. */
export const SEVERITY_ORDER: Severity[] = ["error", "warning", "info"];

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

export function eventLabel(event: string): string {
  return EVENT_LABELS[event] ?? event;
}

export function isBusy(status: DocumentStatus): boolean {
  return status === "queued" || status === "processing";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kilobytes = Math.round(bytes / 1024);
  if (kilobytes < 1024) return `${kilobytes} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatPercent(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${(seconds / 60).toFixed(1)} min`;
}

/** The time a finished document took, or null if it has not finished. */
export function processingSeconds(started: string | null, finished: string | null): number | null {
  if (!started || !finished) return null;
  const seconds = (new Date(finished).getTime() - new Date(started).getTime()) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

export type ConfidenceLevel = "high" | "medium" | "low";

/** The thresholds match validation: below 0.6 raises a low-confidence issue. */
export function confidenceLevel(confidence: number): ConfidenceLevel {
  if (confidence >= 0.85) return "high";
  if (confidence >= 0.6) return "medium";
  return "low";
}

export function openIssueCount(result: InvoiceResult): number {
  return result.validation_errors.filter((issue) => issue.severity !== "info" && !issue.is_resolved)
    .length;
}

/** A label for one invoice among several in a file. */
export function invoiceTabLabel(result: InvoiceResult): string {
  return result.fields.invoice_number.value ?? `Invoice ${result.invoice_index + 1}`;
}

export function pageRangeLabel(start: number | null, end: number | null): string {
  if (start === null) return "";
  return end === null || end === start ? `Page ${start}` : `Pages ${start}–${end}`;
}

/** Spreadsheet text has one row per line, with cells joined by " | ". */
export function parseSheetText(text: string): { title: string | null; rows: string[][] } {
  const lines = text.split("\n").filter((line) => line.trim() !== "");
  const title = lines[0]?.startsWith("Sheet: ") ? lines[0].slice(7) : null;
  const body = title === null ? lines : lines.slice(1);
  return { title, rows: body.map((line) => line.split(" | ")) };
}
