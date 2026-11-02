"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type DocumentFilters,
  type DocumentList,
  type DocumentSummary,
  getMetrics,
  listDocuments,
  type Metrics,
  reprocessFailed,
} from "@/lib/api";
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  isBusy,
  processingSeconds,
  stageLabel,
} from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { UploadPanel } from "./UploadPanel";
import {
  Button,
  Card,
  ErrorNote,
  FormLabel,
  INPUT_CLASS,
  ProgressBar,
  ReviewBadge,
  StatusBadge,
} from "./ui";

const PAGE_SIZE = 25;
const FAST_POLL_MS = 3000;
const SLOW_POLL_MS = 15000;
const TEXT_FILTER_DELAY_MS = 300;

const FILTER_KEYS: (keyof DocumentFilters)[] = [
  "status",
  "needs_review",
  "file_type",
  "filename",
  "supplier",
  "invoice_number",
  "date_from",
  "date_to",
];

function readFilters(params: URLSearchParams): DocumentFilters {
  const filters: DocumentFilters = {};
  for (const key of FILTER_KEYS) {
    const value = params.get(key);
    if (value) filters[key] = value;
  }
  return filters;
}

export function Dashboard() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryString = params.toString();
  // Filters live in the URL, so they survive opening a document and pressing Back.
  const filters = useMemo(() => readFilters(new URLSearchParams(queryString)), [queryString]);
  const offset = Math.max(0, Number(params.get("offset")) || 0);

  const setFilter = useCallback(
    (key: keyof DocumentFilters, value: string) => {
      const next = new URLSearchParams(queryString);
      if (value) next.set(key, value);
      else next.delete(key);
      next.delete("offset");
      const query = next.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, queryString, router],
  );

  const clearFilters = useCallback(() => {
    router.replace(pathname, { scroll: false });
  }, [pathname, router]);

  const setOffset = (value: number) => {
    const next = new URLSearchParams(queryString);
    if (value > 0) next.set("offset", String(value));
    else next.delete("offset");
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  const load = useCallback(
    (signal: AbortSignal) => listDocuments(filters, PAGE_SIZE, offset, signal),
    [filters, offset],
  );
  const interval = useCallback(
    (data: DocumentList | null) =>
      data?.items.some((item) => isBusy(item.status)) ? FAST_POLL_MS : SLOW_POLL_MS,
    [],
  );
  const documents = usePolling(load, interval);

  const loadMetrics = useCallback((signal: AbortSignal) => getMetrics(signal), []);
  const metrics = usePolling(loadMetrics, FAST_POLL_MS * 2);

  const refreshAll = useCallback(() => {
    documents.refresh();
    metrics.refresh();
  }, [documents, metrics]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Documents</h1>
        <p className="text-sm text-slate-600">
          Upload invoices, follow their progress, and review what was extracted.
        </p>
      </div>

      <UploadPanel onUploaded={refreshAll} />
      <MetricsStrip metrics={metrics.data} />

      <div className="flex flex-col gap-3">
        <FilterBar filters={filters} onChange={setFilter} onClear={clearFilters} />
        <RetryFailedBar metrics={metrics.data} onDone={refreshAll} />
        {documents.error && <ErrorNote>{documents.error}</ErrorNote>}
        <DocumentTable data={documents.data} loading={documents.loading} />
        {documents.data && documents.data.total > PAGE_SIZE && (
          <Pagination
            total={documents.data.total}
            offset={offset}
            onChange={setOffset}
          />
        )}
      </div>
    </div>
  );
}

function MetricsStrip({ metrics }: { metrics: Metrics | null }) {
  const byStatus = metrics?.documents_by_status ?? {};
  const active = (byStatus.queued ?? 0) + (byStatus.processing ?? 0);
  const items = [
    { label: "Documents", value: metrics ? String(metrics.documents_total) : "—" },
    { label: "In progress", value: metrics ? String(active) : "—" },
    { label: "Needs review", value: metrics ? String(metrics.needs_review) : "—" },
    { label: "Failed", value: metrics ? String(byStatus.failed ?? 0) : "—" },
    { label: "Success rate", value: metrics ? formatPercent(metrics.success_rate) : "—" },
    {
      label: "Average time",
      value: metrics ? formatDuration(metrics.processing_time.mean_seconds) : "—",
    },
  ];
  return (
    <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-slate-200 bg-slate-200 sm:grid-cols-3 lg:grid-cols-6">
      {items.map((item) => (
        <div key={item.label} className="flex flex-col gap-0.5 bg-white px-4 py-3">
          <dt className="text-xs text-slate-500">{item.label}</dt>
          <dd className="text-lg font-semibold tabular-nums text-slate-900">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function FilterText({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [draftFor, setDraftFor] = useState(value);
  const [lastSent, setLastSent] = useState(value);
  if (draftFor !== value) {
    setDraftFor(value);
    // Only a change from outside, as with "Clear filters", moves the box. This filter's own value
    // coming back from the URL must not overwrite what was typed in the meantime.
    if (value !== lastSent) setDraft(value);
  }

  useEffect(() => {
    if (draft === value) return undefined;
    const timer = setTimeout(() => {
      setLastSent(draft);
      onChange(draft);
    }, TEXT_FILTER_DELAY_MS);
    return () => clearTimeout(timer);
  }, [draft, onChange, value]);

  return (
    <FormLabel label={label}>
      <input
        className={`${INPUT_CLASS} w-full`}
        value={draft}
        placeholder={placeholder}
        onChange={(event) => setDraft(event.target.value)}
      />
    </FormLabel>
  );
}

function FilterBar({
  filters,
  onChange,
  onClear,
}: {
  filters: DocumentFilters;
  onChange: (key: keyof DocumentFilters, value: string) => void;
  onClear: () => void;
}) {
  const hasFilters = FILTER_KEYS.some((key) => filters[key]);
  return (
    <Card className="grid grid-cols-2 items-end gap-3 p-3 md:grid-cols-4 xl:grid-cols-8">
      <FormLabel label="Status">
        <select
          className={INPUT_CLASS}
          value={filters.status ?? ""}
          onChange={(event) => onChange("status", event.target.value)}
        >
          <option value="">Any</option>
          <option value="queued">Queued</option>
          <option value="processing">Processing</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
        </select>
      </FormLabel>
      <FormLabel label="Review">
        <select
          className={INPUT_CLASS}
          value={filters.needs_review ?? ""}
          onChange={(event) => onChange("needs_review", event.target.value)}
        >
          <option value="">Any</option>
          <option value="true">Needs review</option>
          <option value="false">No review needed</option>
        </select>
      </FormLabel>
      <FormLabel label="File type">
        <select
          className={INPUT_CLASS}
          value={filters.file_type ?? ""}
          onChange={(event) => onChange("file_type", event.target.value)}
        >
          <option value="">Any</option>
          <option value="pdf">PDF</option>
          <option value="jpeg">JPG</option>
          <option value="png">PNG</option>
          <option value="tiff">TIFF</option>
          <option value="xlsx">XLSX</option>
          <option value="xls">XLS</option>
          <option value="csv">CSV</option>
        </select>
      </FormLabel>
      <FilterText
        label="File name"
        value={filters.filename ?? ""}
        onChange={(value) => onChange("filename", value)}
      />
      <FilterText
        label="Supplier"
        value={filters.supplier ?? ""}
        onChange={(value) => onChange("supplier", value)}
      />
      <FilterText
        label="Invoice number"
        value={filters.invoice_number ?? ""}
        onChange={(value) => onChange("invoice_number", value)}
      />
      <FormLabel label="Invoice date from">
        <input
          type="date"
          className={INPUT_CLASS}
          value={filters.date_from ?? ""}
          onChange={(event) => onChange("date_from", event.target.value)}
        />
      </FormLabel>
      <FormLabel label="Invoice date to">
        <input
          type="date"
          className={INPUT_CLASS}
          value={filters.date_to ?? ""}
          onChange={(event) => onChange("date_to", event.target.value)}
        />
      </FormLabel>
      {hasFilters && (
        <div className="col-span-full">
          <Button variant="ghost" onClick={onClear}>
            Clear filters
          </Button>
        </div>
      )}
    </Card>
  );
}

function RetryFailedBar({ metrics, onDone }: { metrics: Metrics | null; onDone: () => void }) {
  const [state, setState] = useState<"idle" | "working">("idle");
  const [message, setMessage] = useState<string | null>(null);
  const failed = metrics?.documents_by_status.failed ?? 0;
  if (failed === 0 && !message) return null;

  const retry = async () => {
    setState("working");
    setMessage(null);
    try {
      const result = await reprocessFailed();
      setMessage(
        result.failed_to_queue > 0
          ? `Queued ${result.queued}. ${result.failed_to_queue} could not be queued, so try again shortly.`
          : `Queued ${result.queued} for another attempt.`,
      );
      onDone();
    } catch (problem) {
      setMessage(problem instanceof Error ? problem.message : "The retry failed.");
    } finally {
      setState("idle");
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      {failed > 0 && (
        <Button onClick={() => void retry()} disabled={state === "working"}>
          {state === "working" ? "Retrying…" : `Retry ${failed} failed`}
        </Button>
      )}
      {message && <p role="status" className="text-slate-600">{message}</p>}
    </div>
  );
}

function DocumentTable({ data, loading }: { data: DocumentList | null; loading: boolean }) {
  if (!data) {
    return (
      <Card className="px-4 py-10 text-center text-sm text-slate-500">
        {loading ? "Loading documents…" : "No documents to show."}
      </Card>
    );
  }
  if (data.items.length === 0) {
    return (
      <Card className="px-4 py-10 text-center text-sm text-slate-500">
        No documents match. Upload a file above, or change the filters.
      </Card>
    );
  }
  return (
    <Card className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs font-medium text-slate-500">
            <th scope="col" className="px-4 py-2.5 font-medium">File</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">Invoices</th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">Pages</th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">Time</th>
            <th scope="col" className="px-4 py-2.5 font-medium">Uploaded</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {data.items.map((item) => (
            <DocumentRow key={item.id} item={item} />
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function DocumentRow({ item }: { item: DocumentSummary }) {
  const busy = isBusy(item.status);
  return (
    <tr className="align-top hover:bg-slate-50">
      <td className="max-w-xs px-4 py-3">
        <Link
          href={`/documents/${item.id}`}
          className="block truncate font-medium text-slate-900 hover:text-indigo-700 hover:underline"
          title={item.filename}
        >
          {item.filename}
        </Link>
        <span className="text-xs uppercase text-slate-500">
          {item.file_type} · {formatBytes(item.size_bytes)}
        </span>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap gap-1">
          <StatusBadge status={item.status} />
          {item.needs_review && <ReviewBadge />}
        </div>
        {busy && (
          <div className="mt-1.5 flex w-40 flex-col gap-1">
            <ProgressBar percent={item.progress} label={`Progress of ${item.filename}`} />
            <span className="text-xs text-slate-500">{stageLabel(item.stage)}</span>
          </div>
        )}
        {item.status === "failed" && item.failure_reason && (
          <p className="mt-1 max-w-xs text-xs text-red-800">{item.failure_reason}</p>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
        {item.invoice_count || "—"}
      </td>
      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
        {item.page_count ?? "—"}
      </td>
      <td className="px-4 py-3 text-right tabular-nums text-slate-700">
        {formatDuration(processingSeconds(item.processing_started_at, item.processing_finished_at))}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-slate-600">
        {formatDateTime(item.created_at)}
      </td>
    </tr>
  );
}

function Pagination({
  total,
  offset,
  onChange,
}: {
  total: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  const end = Math.min(offset + PAGE_SIZE, total);
  return (
    <nav aria-label="Pages" className="flex items-center justify-between text-sm text-slate-600">
      <span>
        {offset + 1}–{end} of {total}
      </span>
      <div className="flex gap-2">
        <Button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </Button>
        <Button disabled={end >= total} onClick={() => onChange(offset + PAGE_SIZE)}>
          Next
        </Button>
      </div>
    </nav>
  );
}
