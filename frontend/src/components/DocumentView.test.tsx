import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentDetail, InvoiceResult } from "@/lib/api";
import { DocumentView } from "./DocumentView";
import {
  advance,
  DOC_ID,
  errorResponse,
  flush,
  makeDetail,
  makeFields,
  makeHistoryEntry,
  makeResult,
  navigation,
  RESULT_ID,
  stubApi,
} from "@/test/fixtures";

vi.mock("next/navigation", async () => (await import("@/test/fixtures")).navigationModule);

const DOCUMENT = `GET /documents/${DOC_ID}`;
const RESULTS = `GET /documents/${DOC_ID}/results`;
const HISTORY = `GET /documents/${DOC_ID}/history`;
const REPROCESS = `POST /documents/${DOC_ID}/reprocess`;
const CORRECT = `PATCH /documents/${DOC_ID}/results/${RESULT_ID}`;

function stubDocument(
  document: DocumentDetail = makeDetail(),
  results: InvoiceResult[] = [makeResult()],
) {
  return stubApi({
    [DOCUMENT]: document,
    [RESULTS]: results,
    [HISTORY]: [],
    [REPROCESS]: { status: "queued" },
  });
}

async function show(path = `/documents/${DOC_ID}`) {
  navigation.reset(path);
  render(<DocumentView documentId={DOC_ID} />);
  await flush();
}

const busyDocument = (overrides: Partial<DocumentDetail> = {}) =>
  makeDetail({
    status: "processing",
    stage: "text_extraction",
    progress: 40,
    invoice_count: 0,
    ...overrides,
  });

const failedDocument = (overrides: Partial<DocumentDetail> = {}) =>
  makeDetail({
    status: "failed",
    failure_reason: "The file is corrupted.",
    is_retryable: true,
    invoice_count: 0,
    ...overrides,
  });

const previewImage = () => screen.getByRole("img");

function twoInvoices() {
  return [
    makeResult({
      id: "r1",
      invoice_index: 0,
      page_start: 1,
      page_end: 2,
      fields: makeFields({
        invoice_number: { value: "INV-A" },
        supplier_name: { value: "First Supplier" },
      }),
    }),
    makeResult({
      id: "r2",
      invoice_index: 1,
      page_start: 3,
      page_end: 3,
      fields: makeFields({
        invoice_number: { value: "INV-B" },
        supplier_name: { value: "Second Supplier" },
      }),
    }),
  ];
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DocumentView: loading", () => {
  it("shows loading, then the content", async () => {
    const answer = Promise.withResolvers<DocumentDetail>();
    stubApi({
      [DOCUMENT]: () => answer.promise,
      [RESULTS]: [makeResult()],
    });
    navigation.reset(`/documents/${DOC_ID}`);
    render(<DocumentView documentId={DOC_ID} />);
    await flush();
    expect(screen.getByText("Loading document…")).toBeInTheDocument();

    await act(async () => answer.resolve(makeDetail({ filename: "march.pdf" })));
    await flush();

    expect(screen.queryByText("Loading document…")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "march.pdf" })).toBeInTheDocument();
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
  });

  it("shows the message and a way back for a 404", async () => {
    stubApi({ [DOCUMENT]: errorResponse("Document not found.", 404) });
    await show();

    expect(screen.getByRole("alert")).toHaveTextContent("Document not found.");
    expect(screen.getByRole("link", { name: "Back to documents" })).toHaveAttribute("href", "/");
  });

  it("shows the header details and a link back", async () => {
    stubDocument(makeDetail({ needs_review: true, page_count: 3, size_bytes: 2048 }));
    await show();

    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("href", "/");
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByText("3 pages")).toBeInTheDocument();
    expect(screen.getByText(/Uploaded .*2026/)).toBeInTheDocument();
  });

  it("keeps the content when a later poll fails, with a note", async () => {
    const api = stubDocument();
    await show();

    api.routes[DOCUMENT] = errorResponse("Temporary problem.", 500);
    await advance(30000);

    expect(screen.getByRole("alert")).toHaveTextContent("Temporary problem.");
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
  });

  it("shows a results error beside the document", async () => {
    stubApi({
      [DOCUMENT]: makeDetail(),
      [RESULTS]: errorResponse("Results are unavailable.", 500),
    });
    await show();

    expect(screen.getByRole("alert")).toHaveTextContent("Results are unavailable.");
    expect(screen.getByRole("heading", { name: "invoice.pdf" })).toBeInTheDocument();
  });

  it("says so when a finished document has no invoices", async () => {
    stubDocument(makeDetail({ invoice_count: 0 }), []);
    await show();

    expect(screen.getByText("No invoice data was found.")).toBeInTheDocument();
  });

  it("stops polling after leaving the page", async () => {
    const api = stubDocument(busyDocument(), []);
    navigation.reset(`/documents/${DOC_ID}`);
    const { unmount } = render(<DocumentView documentId={DOC_ID} />);
    await flush();
    const calls = api.calls(DOCUMENT).length;

    unmount();
    await advance(60000);

    expect(api.calls(DOCUMENT)).toHaveLength(calls);
  });
});

describe("DocumentView: while busy", () => {
  it("shows the progress bar and stage, and no way to requeue", async () => {
    stubDocument(busyDocument(), []);
    await show();

    expect(screen.getByRole("progressbar", { name: "Processing progress" })).toHaveAttribute(
      "aria-valuenow",
      "40",
    );
    expect(screen.getByText("Reading text · 40%")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reprocess" })).not.toBeInTheDocument();
  });

  it("says results are on the way", async () => {
    stubDocument(busyDocument(), []);
    await show();

    expect(
      screen.getByText("Results will appear here as soon as they are ready."),
    ).toBeInTheDocument();
  });

  it("is read-only with partial results", async () => {
    stubDocument(busyDocument({ invoice_count: 1 }), [makeResult()]);
    await show();

    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Edit/ })).not.toBeInTheDocument();
  });

  it("polls every 2 seconds", async () => {
    const api = stubDocument(busyDocument(), []);
    await show();
    expect(api.calls(DOCUMENT)).toHaveLength(1);

    await advance(1999);
    expect(api.calls(DOCUMENT)).toHaveLength(1);
    await advance(1);
    expect(api.calls(DOCUMENT)).toHaveLength(2);
  });

  it("slows to every 30 seconds when finished", async () => {
    const api = stubDocument();
    await show();
    expect(api.calls(DOCUMENT)).toHaveLength(1);

    await advance(29999);
    expect(api.calls(DOCUMENT)).toHaveLength(1);
    await advance(1);
    expect(api.calls(DOCUMENT)).toHaveLength(2);
  });

  it("loads new results as the invoice count rises during a run", async () => {
    const api = stubDocument(busyDocument({ invoice_count: 0 }), []);
    await show();
    expect(api.calls(RESULTS)).toHaveLength(1);

    api.routes[DOCUMENT] = busyDocument({ invoice_count: 1 });
    api.routes[RESULTS] = [makeResult()];
    await advance(2000);

    expect(api.calls(RESULTS)).toHaveLength(2);
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
  });

  it("does not keep refetching results when nothing has changed", async () => {
    const api = stubDocument(busyDocument(), []);
    await show();

    await advance(10000);

    expect(api.calls(RESULTS)).toHaveLength(1);
  });

  it("loads the results when it finishes, and allows editing", async () => {
    const api = stubDocument(busyDocument(), []);
    await show();
    expect(screen.queryByText("Acme Ltd")).not.toBeInTheDocument();

    api.routes[DOCUMENT] = makeDetail();
    api.routes[RESULTS] = [makeResult()];
    await advance(2000);

    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit Supplier" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reprocess" })).toBeInTheDocument();
  });

  // BUG, components/DocumentView.tsx:51-58: loadResults has no ordering or abort. A slow request
  // from during the run can answer after the one from the finish and put the old list back, and
  // nothing fetches again because previousBusy already matches. A finished document then says
  // "No invoice data was found" until the page is reloaded.
  it("does not let a slow results answer from the run replace the final results", async () => {
    const slow = Promise.withResolvers<InvoiceResult[]>();
    const api = stubDocument(busyDocument(), []);
    api.routes[RESULTS] = () => slow.promise;
    await show();

    api.routes[DOCUMENT] = makeDetail();
    api.routes[RESULTS] = [makeResult()];
    await advance(2000);
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();

    await act(async () => slow.resolve([]));

    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
  });
});

describe("DocumentView: several invoices", () => {
  it("has no tabs for a single invoice", async () => {
    stubDocument();
    await show();

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("shows a tab per invoice, the first one chosen", async () => {
    stubDocument(makeDetail({ invoice_count: 2 }), twoInvoices());
    await show();

    expect(screen.getByText("2 invoices in this file")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "INV-A" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "INV-B" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("First Supplier")).toBeInTheDocument();
  });

  it("switches the fields and moves the preview to the invoice's first page", async () => {
    stubDocument(makeDetail({ invoice_count: 2 }), twoInvoices());
    await show();
    expect(previewImage()).toHaveAccessibleName("Page 1 of invoice.pdf");

    fireEvent.click(screen.getByRole("tab", { name: "INV-B" }));

    expect(screen.getByRole("tab", { name: "INV-B" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "INV-A" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("Second Supplier")).toBeInTheDocument();
    expect(screen.queryByText("First Supplier")).not.toBeInTheDocument();
    expect(previewImage()).toHaveAccessibleName("Page 3 of invoice.pdf");
  });

  it("leaves the preview alone for an invoice with no known page", async () => {
    const results = twoInvoices();
    results[1].page_start = null;
    stubDocument(makeDetail({ invoice_count: 2 }), results);
    await show();

    fireEvent.click(screen.getByRole("tab", { name: "INV-B" }));

    expect(previewImage()).toHaveAccessibleName("Page 1 of invoice.pdf");
  });

  it("shows the page range on a tab", async () => {
    stubDocument(makeDetail({ invoice_count: 2 }), twoInvoices());
    await show();

    expect(screen.getByRole("tab", { name: "INV-A" })).toHaveAttribute("title", "Pages 1–2");
    expect(screen.getByRole("tab", { name: "INV-B" })).toHaveAttribute("title", "Page 3");
  });

  it("numbers an invoice with no invoice number", async () => {
    const results = twoInvoices();
    results[1].fields = makeFields({ invoice_number: { value: null } });
    stubDocument(makeDetail({ invoice_count: 2 }), results);
    await show();

    expect(screen.getByRole("tab", { name: "Invoice 2" })).toBeInTheDocument();
  });

  it("does not lose a correction on switching invoices", async () => {
    const results = twoInvoices();
    const corrected = {
      ...results[1],
      fields: makeFields({
        invoice_number: { value: "INV-B" },
        supplier_name: { value: "Corrected Supplier" },
      }),
    };
    const api = stubDocument(makeDetail({ invoice_count: 2 }), results);
    api.routes[`PATCH /documents/${DOC_ID}/results/r2`] = corrected;
    await show();

    fireEvent.click(screen.getByRole("tab", { name: "INV-B" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit Supplier" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Supplier" }), {
      target: { value: "Corrected Supplier" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await flush();
    fireEvent.click(screen.getByRole("tab", { name: "INV-A" }));
    fireEvent.click(screen.getByRole("tab", { name: "INV-B" }));

    expect(screen.getByText("Corrected Supplier")).toBeInTheDocument();
  });
});

describe("DocumentView: the preview's page", () => {
  it("starts at ?page=", async () => {
    stubDocument();
    await show(`/documents/${DOC_ID}?page=3`);

    expect(previewImage()).toHaveAccessibleName("Page 3 of invoice.pdf");
  });

  it.each(["abc", "0", ""])("starts at page 1 for ?page=%s", async (value) => {
    stubDocument();
    await show(`/documents/${DOC_ID}?page=${value}`);

    expect(previewImage()).toHaveAccessibleName("Page 1 of invoice.pdf");
  });

  // BUG (minor), components/DocumentView.tsx:35: ?page= is not checked against the document, so a
  // stale link shows "Page 99 of 3" with a broken image, and -1 makes a request for page -1.
  it.each(["99", "-1", "1.5"])("keeps ?page=%s within the document", async (value) => {
    stubDocument();
    await show(`/documents/${DOC_ID}?page=${value}`);

    expect(previewImage().getAttribute("alt")).toMatch(/^Page [123] of /);
  });

  it("moves with Next and Previous", async () => {
    stubDocument();
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(previewImage()).toHaveAccessibleName("Page 2 of invoice.pdf");

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(previewImage()).toHaveAccessibleName("Page 1 of invoice.pdf");
  });

  it("moves to the page a field came from", async () => {
    stubDocument(makeDetail(), [
      makeResult({ fields: makeFields({ total: { source_page: 2 } }) }),
    ]);
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Page 2" }));

    expect(previewImage()).toHaveAccessibleName("Page 2 of invoice.pdf");
  });
});

describe("DocumentView: corrections", () => {
  it("shows the corrected value and refreshes the document's review state", async () => {
    const api = stubDocument(makeDetail({ needs_review: true }));
    api.routes[CORRECT] = makeResult({
      fields: makeFields({ supplier_name: { value: "New Supplier" } }),
    });
    await show();
    expect(screen.getByText("Needs review")).toBeInTheDocument();

    api.routes[DOCUMENT] = makeDetail({ needs_review: false });
    fireEvent.click(screen.getByRole("button", { name: "Edit Supplier" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Supplier" }), {
      target: { value: "New Supplier" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await flush();

    expect(screen.getByText("New Supplier")).toBeInTheDocument();
    expect(screen.queryByText("Needs review")).not.toBeInTheDocument();
  });
});

describe("DocumentView: failed documents", () => {
  it("shows the reason", async () => {
    stubDocument(failedDocument(), []);
    await show();

    expect(screen.getByRole("alert")).toHaveTextContent("The file is corrupted.");
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(
      screen.getByText("This document failed before any invoice data was extracted."),
    ).toBeInTheDocument();
  });

  it("Retry calls the API with resume=true at once, with no confirmation", async () => {
    const api = stubDocument(failedDocument(), []);
    await show();

    api.routes[DOCUMENT] = busyDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await flush();

    expect(api.calls(REPROCESS)).toHaveLength(1);
    expect(api.calls(REPROCESS)[0].query).toEqual({ resume: "true" });
    expect(screen.getByRole("progressbar", { name: "Processing progress" })).toBeInTheDocument();
    expect(screen.queryByText("The file is corrupted.")).not.toBeInTheDocument();
  });

  it("offers no Retry on a completed document", async () => {
    stubDocument();
    await show();

    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("shows why a retry could not start", async () => {
    const api = stubDocument(failedDocument(), []);
    api.routes[REPROCESS] = errorResponse("The queue is down.", 503);
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await flush();

    expect(screen.getByText("The queue is down.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });
});

describe("DocumentView: reprocess", () => {
  const dialog = () => within(screen.getByRole("alertdialog", { name: "Confirm reprocessing" }));

  it("asks first, warning that corrections are replaced, and calls nothing yet", async () => {
    const api = stubDocument();
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    expect(dialog().getByText(/including any corrections/)).toBeInTheDocument();
    expect(api.calls(REPROCESS)).toHaveLength(0);
  });

  it("calls the API with resume=false only after confirming", async () => {
    const api = stubDocument();
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    api.routes[DOCUMENT] = busyDocument();
    api.routes[RESULTS] = [];
    fireEvent.click(dialog().getByRole("button", { name: "Reprocess from scratch" }));
    await flush();

    expect(api.calls(REPROCESS)).toHaveLength(1);
    expect(api.calls(REPROCESS)[0].query).toEqual({ resume: "false" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Processing progress" })).toBeInTheDocument();
  });

  it("removes the old results after requeueing", async () => {
    const api = stubDocument();
    await show();
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    api.routes[DOCUMENT] = busyDocument();
    api.routes[RESULTS] = [];
    fireEvent.click(dialog().getByRole("button", { name: "Reprocess from scratch" }));
    await flush();

    expect(screen.queryByText("Acme Ltd")).not.toBeInTheDocument();
    expect(
      screen.getByText("Results will appear here as soon as they are ready."),
    ).toBeInTheDocument();
  });

  it("goes back to the first invoice after requeueing", async () => {
    const api = stubDocument(makeDetail({ invoice_count: 2 }), twoInvoices());
    await show();
    fireEvent.click(screen.getByRole("tab", { name: "INV-B" }));

    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));
    fireEvent.click(dialog().getByRole("button", { name: "Reprocess from scratch" }));
    await flush();

    expect(api.calls(REPROCESS)).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "INV-A" })).toHaveAttribute("aria-selected", "true");
  });

  it("Cancel closes the question and calls nothing", async () => {
    const api = stubDocument();
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    fireEvent.click(dialog().getByRole("button", { name: "Cancel" }));
    await flush();

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.calls(REPROCESS)).toHaveLength(0);
  });

  it("stays open with the message when the API refuses", async () => {
    const api = stubDocument();
    api.routes[REPROCESS] = errorResponse("Already processing.", 409);
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    fireEvent.click(dialog().getByRole("button", { name: "Reprocess from scratch" }));
    await flush();

    expect(screen.getByRole("alert")).toHaveTextContent("Already processing.");
    expect(dialog().getByRole("button", { name: "Reprocess from scratch" })).toBeEnabled();
  });

  it("disables the buttons while starting", async () => {
    const answer = Promise.withResolvers<unknown>();
    const api = stubDocument();
    api.routes[REPROCESS] = () => answer.promise;
    await show();
    fireEvent.click(screen.getByRole("button", { name: "Reprocess" }));

    fireEvent.click(dialog().getByRole("button", { name: "Reprocess from scratch" }));
    await flush();

    expect(dialog().getByRole("button", { name: "Starting…" })).toBeDisabled();
    expect(dialog().getByRole("button", { name: "Cancel" })).toBeDisabled();

    await act(async () => answer.resolve({}));
    await flush();
  });
});

describe("DocumentView: history", () => {
  const historyButton = () => screen.getByRole("button", { name: /^History/ });

  it("is collapsed and not fetched until opened", async () => {
    const api = stubDocument();
    await show();

    expect(historyButton()).toHaveAttribute("aria-expanded", "false");
    expect(api.calls(HISTORY)).toHaveLength(0);
  });

  it("opens to show the newest first, with stages, durations and before and after", async () => {
    const api = stubDocument();
    api.routes[HISTORY] = [
      makeHistoryEntry({ id: 1, event: "uploaded" }),
      makeHistoryEntry({
        id: 2,
        event: "stage_completed",
        stage: "text_extraction",
        duration_ms: 1500,
      }),
      makeHistoryEntry({
        id: 3,
        event: "manual_correction",
        actor: "reviewer",
        message: "Corrected invoice 0.",
        before: { total: "100.00" },
        after: { total: "120.00" },
      }),
    ];
    await show();

    fireEvent.click(historyButton());
    expect(historyButton()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await flush();

    const items = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Reviewed by a person");
    expect(items[0]).toHaveTextContent("Corrected invoice 0.");
    expect(items[0]).toHaveTextContent('reviewer: {"total":"100.00"} → {"total":"120.00"}');
    expect(items[1]).toHaveTextContent("Finished: Reading text (1.5 s)");
    expect(items[2]).toHaveTextContent("Uploaded");
  });

  it("closes again", async () => {
    const api = stubDocument();
    api.routes[HISTORY] = [makeHistoryEntry({ event: "uploaded", message: "Stored." })];
    await show();

    fireEvent.click(historyButton());
    await flush();
    expect(screen.getByText("Stored.")).toBeInTheDocument();

    fireEvent.click(historyButton());

    expect(screen.queryByText("Stored.")).not.toBeInTheDocument();
    expect(historyButton()).toHaveAttribute("aria-expanded", "false");
  });

  it("shows an error when it cannot be loaded", async () => {
    const api = stubDocument();
    api.routes[HISTORY] = errorResponse("History is unavailable.", 500);
    await show();

    fireEvent.click(historyButton());
    await flush();

    expect(screen.getByRole("alert")).toHaveTextContent("History is unavailable.");
  });

  it("refetches while open when the document changes", async () => {
    const api = stubDocument(busyDocument({ updated_at: "2026-03-15T12:00:00Z" }), []);
    await show();
    fireEvent.click(historyButton());
    await flush();
    expect(api.calls(HISTORY)).toHaveLength(1);

    api.routes[DOCUMENT] = busyDocument({ updated_at: "2026-03-15T12:00:05Z" });
    await advance(2000);

    expect(api.calls(HISTORY)).toHaveLength(2);
  });

  // BUG, components/DocumentView.tsx:139 and 308 with backend/app/api/corrections.py:157-170: open
  // history only refetches when updated_at changes, but a correction adds a history row without
  // touching the document unless needs_review flips. The entry stays hidden until a reload.
  it("shows a correction in open history, with no change to updated_at", async () => {
    const api = stubDocument();
    api.routes[CORRECT] = makeResult({
      fields: makeFields({ supplier_name: { value: "New Supplier" } }),
    });
    await show();
    fireEvent.click(historyButton());
    await flush();
    expect(api.calls(HISTORY)).toHaveLength(1);

    api.routes[HISTORY] = [
      makeHistoryEntry({ event: "manual_correction", message: "Corrected invoice 0." }),
    ];
    fireEvent.click(screen.getByRole("button", { name: "Edit Supplier" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Supplier" }), {
      target: { value: "New Supplier" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await flush();

    expect(screen.getByText("Corrected invoice 0.")).toBeInTheDocument();
  });
});

describe("DocumentView: wording", () => {
  it("says 1 page for a one-page file", async () => {
    stubDocument(makeDetail({ page_count: 1 }));
    await show();

    expect(screen.getByText("1 page")).toBeInTheDocument();
  });
});
