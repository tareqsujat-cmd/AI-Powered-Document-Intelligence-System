"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type DocumentDetail,
  getDocument,
  getHistory,
  getResults,
  type HistoryEntry,
  type InvoiceResult,
  reprocessDocument,
} from "@/lib/api";
import {
  eventLabel,
  formatBytes,
  formatDateTime,
  invoiceTabLabel,
  isBusy,
  openIssueCount,
  pageRangeLabel,
  stageLabel,
} from "@/lib/format";
import { usePolling } from "@/lib/usePolling";
import { ArrowLeftIcon, CheckCircleIcon, ClockIcon, ErrorIcon } from "./icons";
import { InvoicePanel } from "./InvoicePanel";
import { PagePreview } from "./PagePreview";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  FileTypeIcon,
  ProgressBar,
  ReviewBadge,
  StatusBadge,
} from "./ui";

const BUSY_POLL_MS = 2000;
const IDLE_POLL_MS = 30000;

export function DocumentView({ documentId }: { documentId: string }) {
  const params = useSearchParams();
  const requestedPage = Number(params.get("page"));
  const [page, setPage] = useState(
    Number.isInteger(requestedPage) && requestedPage >= 1 ? requestedPage : 1,
  );
  const [results, setResults] = useState<InvoiceResult[] | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [changeCount, setChangeCount] = useState(0);

  const load = useCallback(
    (signal: AbortSignal) => getDocument(documentId, signal),
    [documentId],
  );
  const interval = useCallback(
    (data: DocumentDetail | null) => (data && !isBusy(data.status) ? IDLE_POLL_MS : BUSY_POLL_MS),
    [],
  );
  const { data: document, error, refresh } = usePolling(load, interval);

  const latestResultsRequest = useRef(0);
  const loadResults = useCallback(async () => {
    // A slow answer from during a run must not replace the final results that came after it.
    latestResultsRequest.current += 1;
    const request = latestResultsRequest.current;
    try {
      const loaded = await getResults(documentId);
      if (request !== latestResultsRequest.current) return;
      setResults(loaded);
      setResultsError(null);
    } catch (problem) {
      if (request !== latestResultsRequest.current) return;
      setResultsError(problem instanceof Error ? problem.message : "Results could not be loaded.");
    }
  }, [documentId]);

  // Results are fetched once a run ends, and while one is running there may be none yet.
  const busy = document ? isBusy(document.status) : false;
  const documentLoaded = document !== null;
  const invoiceCount = document?.invoice_count ?? 0;
  const previousBusy = useRef<boolean | null>(null);
  useEffect(() => {
    if (!documentLoaded) return;
    if (previousBusy.current !== busy || (busy && invoiceCount !== results?.length)) {
      previousBusy.current = busy;
      void loadResults();
    }
  }, [busy, documentLoaded, invoiceCount, loadResults, results?.length]);

  if (error && !document) {
    return (
      <div className="flex flex-col gap-3">
        <ErrorNote>{error}</ErrorNote>
        <Link href="/" className="text-sm text-indigo-700 hover:underline">
          Back to documents
        </Link>
      </div>
    );
  }
  if (!document) return <p className="text-sm text-slate-500">Loading document…</p>;

  const current = results?.[Math.min(selected, (results?.length ?? 1) - 1)] ?? null;
  // A link can name a page the file does not have. The length is unknown until it has been read.
  const lastPage = document.page_count ?? document.stored_page_count;
  const shownPage = lastPage > 0 ? Math.min(page, lastPage) : page;

  const showInvoice = (index: number) => {
    setSelected(index);
    const start = results?.[index]?.page_start;
    if (start) setPage(start);
  };

  return (
    <div className="flex flex-col gap-5">
      <DocumentHeader
        document={document}
        onRequeued={() => {
          setResults(null);
          setSelected(0);
          previousBusy.current = null;
          refresh();
        }}
      />
      {error && <ErrorNote>{error}</ErrorNote>}

      <div className="grid items-start gap-5 lg:grid-cols-2">
        <div className="lg:sticky lg:top-20">
          <PagePreview document={document} page={shownPage} onPageChange={setPage} />
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          {resultsError && <ErrorNote>{resultsError}</ErrorNote>}
          {results && results.length > 1 && (
            <InvoiceTabs results={results} selected={selected} onSelect={showInvoice} />
          )}
          {current ? (
            <InvoicePanel
              key={current.id}
              documentId={document.id}
              result={current}
              readOnly={busy}
              onResultChanged={(updated) =>
                setResults((all) =>
                  all ? all.map((result) => (result.id === updated.id ? updated : result)) : all,
                )
              }
              onReviewChanged={() => {
                setChangeCount((count) => count + 1);
                refresh();
              }}
              onShowPage={setPage}
            />
          ) : (
            <EmptyState
              icon={
                busy ? (
                  <ClockIcon className="size-6" />
                ) : document.status === "failed" ? (
                  <ErrorIcon className="size-6" />
                ) : (
                  <CheckCircleIcon className="size-6" />
                )
              }
            >
              {busy
                ? "Results will appear here as soon as they are ready."
                : document.status === "failed"
                  ? "This document failed before any invoice data was extracted."
                  : "No invoice data was found."}
            </EmptyState>
          )}
          <HistoryPanel documentId={document.id} refreshKey={`${document.updated_at}-${changeCount}`} />
        </div>
      </div>
    </div>
  );
}

function DocumentHeader({
  document,
  onRequeued,
}: {
  document: DocumentDetail;
  onRequeued: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = isBusy(document.status);

  const run = async (resume: boolean) => {
    setWorking(true);
    setError(null);
    try {
      await reprocessDocument(document.id, resume);
      setConfirming(false);
      onRequeued();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The document could not be requeued.");
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <Link
        href="/"
        className="inline-flex w-fit items-center gap-1.5 text-sm font-medium text-slate-600 hover:text-indigo-700"
      >
        <ArrowLeftIcon className="size-4" />
        Documents
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <FileTypeIcon type={document.file_type} size="lg" />
          <div className="flex min-w-0 flex-col gap-1">
          <h1 className="break-words text-2xl font-semibold tracking-tight">{document.filename}</h1>
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <StatusBadge status={document.status} />
            {document.needs_review && <ReviewBadge />}
            <span className="uppercase">{document.file_type}</span>
            <span>{formatBytes(document.size_bytes)}</span>
            {document.page_count !== null && (
              <span>
                {document.page_count} {document.page_count === 1 ? "page" : "pages"}
              </span>
            )}
            <span>Uploaded {formatDateTime(document.created_at)}</span>
          </div>
          </div>
        </div>
        {!busy && (
          <div className="flex flex-wrap items-center gap-2">
            {document.status === "failed" && document.is_retryable && (
              <Button variant="primary" disabled={working} onClick={() => void run(true)}>
                Retry
              </Button>
            )}
            <Button disabled={working} onClick={() => setConfirming(true)}>
              Reprocess
            </Button>
          </div>
        )}
      </div>

      {busy && (
        <div className="flex max-w-md flex-col gap-1">
          <ProgressBar percent={document.progress} label="Processing progress" />
          <span className="text-xs text-slate-600">
            {stageLabel(document.stage)} · {document.progress}%
          </span>
        </div>
      )}
      {document.status === "failed" && document.failure_reason && (
        <ErrorNote>{document.failure_reason}</ErrorNote>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}

      {confirming && (
        <div
          role="alertdialog"
          aria-label="Confirm reprocessing"
          className="flex flex-col gap-3 rounded-xl bg-amber-50 px-4 py-3 text-sm text-amber-950 ring-1 ring-inset ring-amber-300"
        >
          <p>
            This reads the whole file again and extracts it from scratch, replacing the current
            results, including any corrections made to them. It can use LLM quota and take a while
            on a long scan.
          </p>
          <div className="flex gap-2">
            <Button variant="danger" disabled={working} onClick={() => void run(false)}>
              {working ? "Starting…" : "Reprocess from scratch"}
            </Button>
            <Button disabled={working} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function InvoiceTabs({
  results,
  selected,
  onSelect,
}: {
  results: InvoiceResult[];
  selected: number;
  onSelect: (index: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-medium text-slate-600">{results.length} invoices in this file</p>
      <div role="tablist" className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
        {results.map((result, index) => {
          const open = openIssueCount(result);
          const active = index === selected;
          return (
            <button
              key={result.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelect(index)}
              title={pageRangeLabel(result.page_start, result.page_end)}
              className={`rounded-lg px-2.5 py-1 text-sm font-medium ring-1 ring-inset transition-colors ${
                active
                  ? "bg-indigo-600 text-white ring-indigo-600"
                  : "bg-white text-slate-700 ring-slate-300 hover:bg-slate-50"
              }`}
            >
              {invoiceTabLabel(result)}
              {open > 0 && (
                <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 text-xs text-amber-900">
                  {open}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function HistoryPanel({ documentId, refreshKey }: { documentId: string; refreshKey: string }) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    getHistory(documentId, controller.signal)
      .then((value) => {
        setEntries(value);
        setError(null);
      })
      .catch((problem: unknown) => {
        if (!controller.signal.aborted) {
          setError(problem instanceof Error ? problem.message : "History could not be loaded.");
        }
      });
    return () => controller.abort();
  }, [documentId, open, refreshKey]);

  return (
    <Card>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between rounded-xl px-4 py-3 text-left text-sm font-semibold hover:bg-slate-50"
      >
        History
        <span aria-hidden="true" className="text-slate-400">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="border-t border-slate-200">
          {error && <ErrorNote>{error}</ErrorNote>}
          {!error && !entries && <p className="px-4 py-3 text-sm text-slate-500">Loading…</p>}
          {entries && (
            <ol className="divide-y divide-slate-100">
              {[...entries].reverse().map((entry) => (
                <li key={entry.id} className="flex flex-col gap-0.5 px-4 py-2 text-xs">
                  <div className="flex flex-wrap justify-between gap-2">
                    <span className="font-medium text-slate-800">
                      {eventLabel(entry.event)}
                      {entry.stage ? `: ${stageLabel(entry.stage)}` : ""}
                      {entry.duration_ms !== null ? ` (${(entry.duration_ms / 1000).toFixed(1)} s)` : ""}
                    </span>
                    <span className="text-slate-500">{formatDateTime(entry.created_at)}</span>
                  </div>
                  {entry.message && <p className="text-slate-600">{entry.message}</p>}
                  {entry.before && (
                    <p className="break-words font-mono text-slate-600">
                      {entry.actor}: {JSON.stringify(entry.before)} → {JSON.stringify(entry.after)}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </Card>
  );
}
