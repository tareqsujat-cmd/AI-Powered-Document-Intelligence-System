import { act } from "@testing-library/react";
import { useMemo, useSyncExternalStore } from "react";
import { vi } from "vitest";
import type {
  DocumentDetail,
  DocumentList,
  DocumentSummary,
  ExtractedField,
  FieldName,
  HistoryEntry,
  InvoiceResult,
  LineItem,
  Metrics,
  PageContent,
  SearchHit,
  UploadOutcome,
  ValidationIssue,
} from "@/lib/api";

/* ------------------------------------------------------------------ */
/* next/navigation: a small stateful fake, so URL-driven state works.  */
/* ------------------------------------------------------------------ */

let currentUrl = new URL("http://app.test/");
let heldUrl: string | null = null;
let holding = false;
const listeners = new Set<() => void>();

function applyUrl(href: string) {
  currentUrl = new URL(href, currentUrl);
  listeners.forEach((listener) => listener());
}

function navigate(href: string) {
  if (holding) heldUrl = href;
  else applyUrl(href);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const router = {
  replace: vi.fn((href: string) => navigate(href)),
  push: vi.fn((href: string) => navigate(href)),
};

/** Drives the URL from a test. Pass `navigationModule` to `vi.mock("next/navigation", ...)`. */
export const navigation = {
  router,
  /** The URL as a person would see it, such as `/?status=failed`. */
  get path(): string {
    return currentUrl.pathname + currentUrl.search;
  },
  get params(): URLSearchParams {
    return new URLSearchParams(currentUrl.search);
  },
  /** Start a test at this URL, with no navigation recorded. */
  reset(path = "/") {
    currentUrl = new URL(path, "http://app.test/");
    heldUrl = null;
    holding = false;
    router.replace.mockClear();
    router.push.mockClear();
  },
  /** The URL changes from outside the component, as with the browser's Back button. */
  goTo(path: string) {
    applyUrl(path);
  },
  /** A real router takes a server round trip, so hold navigations until `release`. */
  hold() {
    holding = true;
  },
  release() {
    holding = false;
    if (heldUrl !== null) applyUrl(heldUrl);
    heldUrl = null;
  },
};

export const navigationModule = {
  useRouter: () => router,
  usePathname: () => useSyncExternalStore(subscribe, () => currentUrl.pathname),
  useSearchParams: () => {
    const search = useSyncExternalStore(subscribe, () => currentUrl.search);
    return useMemo(() => new URLSearchParams(search), [search]);
  },
};

/* ------------------------------------------------------------------ */
/* fetch: a small URL-routing fake.                                    */
/* ------------------------------------------------------------------ */

/**
 * Only what the app reads from a Response. A real Response reads its body through streams, which
 * do not settle predictably under fake timers.
 */
export class FakeResponse {
  readonly headers: Headers;
  constructor(
    private readonly body: unknown,
    readonly status = 200,
    headers: Record<string, string> = {},
  ) {
    this.headers = new Headers(headers);
  }
  get ok() {
    return this.status >= 200 && this.status < 300;
  }
  json() {
    return Promise.resolve(this.body);
  }
}

export function errorResponse(
  detail: string,
  status: number,
  headers: Record<string, string> = {},
): FakeResponse {
  return new FakeResponse({ detail }, status, headers);
}

export interface ApiCall {
  url: URL;
  init: RequestInit | undefined;
  /** The search parameters as a plain object. */
  query: Record<string, string>;
  /** The parsed JSON body, if there is one. */
  body: unknown;
}

/** A body, a FakeResponse, or a function returning either, possibly later. Throw to fail it. */
export type RouteHandler = unknown | ((call: ApiCall) => unknown);

export interface FakeApi {
  routes: Record<string, RouteHandler>;
  /** Every request to a route such as `GET /documents`. */
  calls: (route: string) => ApiCall[];
  fetch: ReturnType<typeof vi.fn<typeof fetch>>;
}

function rejectOnAbort<T>(value: Promise<T>, signal: AbortSignal | null | undefined): Promise<T> {
  if (!signal) return value;
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(new DOMException("The operation was aborted.", "AbortError"));
    if (signal.aborted) {
      abort();
      return;
    }
    signal.addEventListener("abort", abort, { once: true });
    value.then(resolve, reject);
  });
}

/**
 * Stubs global fetch. Routes are `METHOD /path` with the `/api/v1` prefix left out and no query.
 * A request honours its abort signal, as a real fetch does. An unknown route answers 404, so a
 * wrong URL fails the test instead of hanging it.
 */
export function stubApi(routes: Record<string, RouteHandler> = {}): FakeApi {
  const history: { route: string; call: ApiCall }[] = [];
  const table = { ...routes };

  const fetchMock = vi.fn<typeof fetch>((input, init) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const route = `${init?.method ?? "GET"} ${path}`;
    const call: ApiCall = {
      url,
      init,
      query: Object.fromEntries(url.searchParams.entries()),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    history.push({ route, call });

    const answer = async () => {
      if (!(route in table)) return errorResponse(`No fake route for ${route}`, 404);
      const handler = table[route];
      const value = await (typeof handler === "function"
        ? (handler as (call: ApiCall) => unknown)(call)
        : handler);
      return value instanceof FakeResponse ? value : new FakeResponse(value);
    };
    return rejectOnAbort(answer(), init?.signal).then((value) => value as unknown as Response);
  });

  vi.stubGlobal("fetch", fetchMock);
  return {
    routes: table,
    fetch: fetchMock,
    calls: (route) => history.filter((entry) => entry.route === route).map((entry) => entry.call),
  };
}

/* ------------------------------------------------------------------ */
/* XMLHttpRequest, for uploads.                                        */
/* ------------------------------------------------------------------ */

export class FakeXhr {
  static instances: FakeXhr[] = [];

  method = "";
  url = "";
  status = 0;
  responseText = "";
  sent: FormData | null = null;
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private responseHeaders = new Headers();

  constructor() {
    FakeXhr.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  send(body: FormData) {
    this.sent = body;
  }

  getResponseHeader(name: string) {
    return this.responseHeaders.get(name);
  }

  progress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as ProgressEvent);
  }

  respond(status: number, body: unknown, headers: Record<string, string> = {}) {
    this.status = status;
    this.responseText = typeof body === "string" ? body : JSON.stringify(body);
    this.responseHeaders = new Headers(headers);
    this.onload?.();
  }

  failNetwork() {
    this.onerror?.();
  }
}

export function stubXhr() {
  FakeXhr.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
}

/* ------------------------------------------------------------------ */
/* Fake timers.                                                        */
/* ------------------------------------------------------------------ */

/** Move fake time on and let everything that was waiting on it, or on a promise, settle. */
export async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Settle pending promises without moving fake time. */
export function flush() {
  return advance(0);
}

/* ------------------------------------------------------------------ */
/* API data.                                                           */
/* ------------------------------------------------------------------ */

export const DOC_ID = "doc-1";
export const RESULT_ID = "res-1";

export function makeSummary(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: DOC_ID,
    filename: "invoice.pdf",
    file_type: "pdf",
    size_bytes: 2048,
    page_count: 3,
    status: "completed",
    stage: "finished",
    progress: 100,
    needs_review: false,
    failure_reason: null,
    is_retryable: false,
    attempt_count: 1,
    created_at: "2026-03-15T12:00:00Z",
    updated_at: "2026-03-15T12:01:00Z",
    processing_started_at: "2026-03-15T12:00:10Z",
    processing_finished_at: "2026-03-15T12:00:20Z",
    invoice_count: 1,
    ...overrides,
  };
}

export function makeDetail(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    ...makeSummary(),
    stored_page_count: 3,
    validation_error_count: 0,
    ...overrides,
  };
}

export function makeList(items: DocumentSummary[], overrides: Partial<DocumentList> = {}) {
  return { items, total: items.length, limit: 25, offset: 0, ...overrides };
}

export function makeMetrics(overrides: Partial<Metrics> = {}): Metrics {
  return {
    documents_total: 10,
    documents_by_status: { completed: 7, queued: 1, processing: 1, failed: 1 },
    needs_review: 2,
    success_rate: 0.875,
    processing_time: { mean_seconds: 12.5, p95_seconds: 30 },
    ...overrides,
  };
}

export function makeField(overrides: Partial<ExtractedField> = {}): ExtractedField {
  return {
    value: "value",
    confidence: 0.9,
    reason: null,
    source_snippet: null,
    source_page: null,
    ...overrides,
  };
}

export function makeFields(
  overrides: Partial<Record<FieldName, Partial<ExtractedField>>> = {},
): Record<FieldName, ExtractedField> {
  const values: Record<FieldName, string> = {
    invoice_number: "INV-001",
    invoice_date: "2026-03-01",
    supplier_name: "Acme Ltd",
    customer_name: "Globex Inc",
    subtotal: "100.00",
    tax: "20.00",
    total: "120.00",
    currency: "USD",
  };
  const fields = {} as Record<FieldName, ExtractedField>;
  for (const name of Object.keys(values) as FieldName[]) {
    fields[name] = makeField({ value: values[name], ...overrides[name] });
  }
  return fields;
}

export function makeLineItem(overrides: Partial<LineItem> = {}): LineItem {
  return {
    id: "line-1",
    position: 0,
    description: "Consulting",
    quantity: "2",
    unit_price: "50.00",
    amount: "100.00",
    confidence: 0.9,
    source_snippet: null,
    source_page: null,
    ...overrides,
  };
}

export function makeIssue(overrides: Partial<ValidationIssue> = {}): ValidationIssue {
  return {
    id: "issue-1",
    code: "total_mismatch",
    severity: "error",
    field: "total",
    message: "Subtotal plus tax does not equal the total.",
    expected_value: null,
    actual_value: null,
    related_document_id: null,
    is_resolved: false,
    ...overrides,
  };
}

export function makeResult(overrides: Partial<InvoiceResult> = {}): InvoiceResult {
  return {
    id: RESULT_ID,
    invoice_index: 0,
    version: 1,
    page_start: 1,
    page_end: 1,
    method: "llm",
    overall_confidence: 0.9,
    warnings: [],
    llm_model: "test-model",
    prompt_version: "v1",
    fields: makeFields(),
    line_items: [],
    validation_errors: [],
    ...overrides,
  };
}

export function makePage(overrides: Partial<PageContent> = {}): PageContent {
  return {
    page_number: 1,
    sheet_name: null,
    source: "spreadsheet",
    status: "completed",
    ocr_confidence: null,
    processing_ms: 1,
    error: null,
    warnings: [],
    text: "",
    ...overrides,
  };
}

export function makeHistoryEntry(overrides: Partial<HistoryEntry> = {}): HistoryEntry {
  return {
    id: 1,
    event: "uploaded",
    stage: null,
    attempt: 1,
    duration_ms: null,
    message: null,
    before: null,
    after: null,
    actor: "system",
    created_at: "2026-03-15T12:00:00Z",
    ...overrides,
  };
}

export function makeHit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    document_id: DOC_ID,
    filename: "invoice.pdf",
    page_number: 2,
    snippet: "Cloud hosting for March",
    score: 0.9,
    matched_by: ["keyword"],
    ...overrides,
  };
}

export function makeOutcome(overrides: Partial<UploadOutcome> = {}): UploadOutcome {
  return {
    filename: "invoice.pdf",
    accepted: true,
    document: makeSummary(),
    error: null,
    ...overrides,
  };
}

export function makeFile(name = "invoice.pdf", size = 2048): File {
  return new File([new Uint8Array(size)], name, { type: "application/octet-stream" });
}
