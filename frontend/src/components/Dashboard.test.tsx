import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentSummary } from "@/lib/api";
import { Dashboard } from "./Dashboard";
import {
  advance,
  type ApiCall,
  errorResponse,
  FakeXhr,
  flush,
  makeFile,
  makeList,
  makeMetrics,
  makeOutcome,
  makeSummary,
  navigation,
  stubApi,
  stubXhr,
} from "@/test/fixtures";

vi.mock("next/navigation", async () => (await import("@/test/fixtures")).navigationModule);

const LIST = "GET /documents";
const METRICS = "GET /metrics";
const RETRY = "POST /documents/reprocess-failed";

const NO_FAILURES = makeMetrics({ documents_by_status: { completed: 10 } });

function stubDashboard(items: DocumentSummary[] = [makeSummary()], metrics = NO_FAILURES) {
  return stubApi({ [LIST]: makeList(items), [METRICS]: metrics });
}

async function show(path = "/") {
  navigation.reset(path);
  render(<Dashboard />);
  await flush();
}

function metric(label: string) {
  const term = screen.getAllByRole("term").find((node) => node.textContent === label);
  return term?.nextElementSibling?.textContent;
}

function rowOf(filename: string) {
  const row = screen.getByRole("link", { name: filename }).closest("tr");
  if (!row) throw new Error(`No row for ${filename}`);
  return within(row);
}

function lastListQuery(api: ReturnType<typeof stubApi>) {
  return api.calls(LIST).at(-1)?.query;
}

function typeInto(label: string, value: string) {
  fireEvent.change(screen.getByRole("textbox", { name: label }), { target: { value } });
}

function pagesOf(total: number) {
  return (call: ApiCall) =>
    makeList([makeSummary({ id: `doc-${call.query.offset}`, filename: `from-${call.query.offset}.pdf` })], {
      total,
      offset: Number(call.query.offset),
    });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Dashboard: rows", () => {
  it("shows a loading message until the list arrives", async () => {
    stubApi({ [LIST]: () => new Promise(() => undefined), [METRICS]: NO_FAILURES });
    await show();

    expect(screen.getByText("Loading documents…")).toBeInTheDocument();
  });

  it("links a file name to its document", async () => {
    stubDashboard([makeSummary({ id: "doc-5", filename: "march.pdf" })]);
    await show();

    expect(screen.getByRole("link", { name: "march.pdf" })).toHaveAttribute(
      "href",
      "/documents/doc-5",
    );
  });

  it("shows the type, size, invoices, pages, time and upload date", async () => {
    stubDashboard([
      makeSummary({
        filename: "a.pdf",
        file_type: "pdf",
        size_bytes: 2048,
        invoice_count: 4,
        page_count: 12,
        processing_started_at: "2026-03-15T12:00:00Z",
        processing_finished_at: "2026-03-15T12:00:30Z",
      }),
    ]);
    await show();

    const row = rowOf("a.pdf");
    expect(row.getByText(/pdf · 2 KB/)).toBeInTheDocument();
    expect(row.getByText("4")).toBeInTheDocument();
    expect(row.getByText("12")).toBeInTheDocument();
    expect(row.getByText("30.0 s")).toBeInTheDocument();
    expect(row.getByText(/2026/)).toBeInTheDocument();
  });

  it("shows dashes for what is not known yet", async () => {
    stubDashboard([
      makeSummary({
        filename: "a.pdf",
        status: "queued",
        invoice_count: 0,
        page_count: null,
        processing_started_at: null,
        processing_finished_at: null,
      }),
    ]);
    await show();

    expect(rowOf("a.pdf").getAllByText("—")).toHaveLength(3);
  });

  it("shows a status, and the review badge only when review is needed", async () => {
    stubDashboard([
      makeSummary({ id: "1", filename: "done.pdf", status: "completed", needs_review: false }),
      makeSummary({ id: "2", filename: "check.pdf", status: "completed", needs_review: true }),
    ]);
    await show();

    expect(rowOf("done.pdf").getByText("Completed")).toBeInTheDocument();
    expect(rowOf("done.pdf").queryByText("Needs review")).not.toBeInTheDocument();
    expect(rowOf("check.pdf").getByText("Needs review")).toBeInTheDocument();
  });

  it("shows the reason on a failed document", async () => {
    stubDashboard([
      makeSummary({ filename: "bad.pdf", status: "failed", failure_reason: "The file is corrupted." }),
    ]);
    await show();

    expect(rowOf("bad.pdf").getByText("Failed")).toBeInTheDocument();
    expect(rowOf("bad.pdf").getByText("The file is corrupted.")).toBeInTheDocument();
  });

  it("shows progress and stage on a busy document, and not on a finished one", async () => {
    stubDashboard([
      makeSummary({
        id: "1",
        filename: "busy.pdf",
        status: "processing",
        stage: "text_extraction",
        progress: 40,
      }),
      makeSummary({ id: "2", filename: "done.pdf" }),
    ]);
    await show();

    expect(screen.getByRole("progressbar", { name: "Progress of busy.pdf" })).toHaveAttribute(
      "aria-valuenow",
      "40",
    );
    expect(rowOf("busy.pdf").getByText("Reading text")).toBeInTheDocument();
    expect(rowOf("done.pdf").queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("says so when no documents match", async () => {
    stubDashboard([]);
    await show();

    expect(
      screen.getByText("No documents match. Upload a file above, or change the filters."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an API error when the list fails", async () => {
    stubApi({ [LIST]: errorResponse("The database is down.", 500), [METRICS]: NO_FAILURES });
    await show();

    expect(screen.getByRole("alert")).toHaveTextContent("The database is down.");
    expect(screen.queryByText("Loading documents…")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("keeps the rows when a later poll fails, and clears the error on recovery", async () => {
    const api = stubDashboard([makeSummary({ filename: "a.pdf" })]);
    await show();

    api.routes[LIST] = errorResponse("Temporary problem.", 500);
    await advance(15000);
    expect(screen.getByRole("alert")).toHaveTextContent("Temporary problem.");
    expect(screen.getByRole("link", { name: "a.pdf" })).toBeInTheDocument();

    api.routes[LIST] = makeList([makeSummary({ filename: "a.pdf" })]);
    await advance(15000);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Dashboard: metrics", () => {
  it("shows dashes before they load", async () => {
    stubApi({ [LIST]: makeList([]), [METRICS]: () => new Promise(() => undefined) });
    await show();

    for (const label of [
      "Documents",
      "In progress",
      "Needs review",
      "Failed",
      "Success rate",
      "Average time",
    ]) {
      expect(metric(label)).toBe("—");
    }
  });

  it("shows the numbers, adding queued and processing together", async () => {
    stubDashboard([], makeMetrics());
    await show();

    expect(metric("Documents")).toBe("10");
    expect(metric("In progress")).toBe("2");
    expect(metric("Needs review")).toBe("2");
    expect(metric("Failed")).toBe("1");
    expect(metric("Success rate")).toBe("88%");
    expect(metric("Average time")).toBe("12.5 s");
  });

  it("shows zero for statuses with no documents, and dashes for no measurements", async () => {
    stubDashboard(
      [],
      makeMetrics({
        documents_total: 0,
        documents_by_status: {},
        needs_review: 0,
        success_rate: null,
        processing_time: { mean_seconds: null, p95_seconds: null },
      }),
    );
    await show();

    expect(metric("Documents")).toBe("0");
    expect(metric("In progress")).toBe("0");
    expect(metric("Failed")).toBe("0");
    expect(metric("Success rate")).toBe("—");
    expect(metric("Average time")).toBe("—");
  });

  it("refreshes every 6 seconds", async () => {
    const api = stubDashboard();
    await show();
    expect(api.calls(METRICS)).toHaveLength(1);

    await advance(5999);
    expect(api.calls(METRICS)).toHaveLength(1);
    await advance(1);
    expect(api.calls(METRICS)).toHaveLength(2);
  });
});

describe("Dashboard: filters", () => {
  it("asks for the first 25 with no filters", async () => {
    const api = stubDashboard();
    await show();

    expect(api.calls(LIST)[0].query).toEqual({ limit: "25", offset: "0" });
  });

  it.each([
    ["Status", "status", "failed"],
    ["Review", "needs_review", "true"],
    ["File type", "file_type", "pdf"],
  ])("the %s select updates the URL and refetches", async (label, key, value) => {
    const api = stubDashboard();
    await show();

    fireEvent.change(screen.getByRole("combobox", { name: label }), { target: { value } });
    await flush();

    expect(navigation.params.get(key)).toBe(value);
    expect(lastListQuery(api)).toMatchObject({ [key]: value, offset: "0" });
    expect(api.calls(LIST)).toHaveLength(2);
  });

  it.each([
    ["Invoice date from", "date_from"],
    ["Invoice date to", "date_to"],
  ])("the %s input updates the URL and refetches", async (label, key) => {
    const api = stubDashboard();
    await show();

    fireEvent.change(screen.getByLabelText(label), { target: { value: "2026-02-03" } });
    await flush();

    expect(navigation.params.get(key)).toBe("2026-02-03");
    expect(lastListQuery(api)).toMatchObject({ [key]: "2026-02-03" });
  });

  it("removes a filter from the URL when set back to Any", async () => {
    const api = stubDashboard();
    await show("/?status=failed&file_type=pdf");

    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "" } });
    await flush();

    expect(navigation.path).toBe("/?file_type=pdf");
    expect(lastListQuery(api)).not.toHaveProperty("status");
    expect(lastListQuery(api)).toMatchObject({ file_type: "pdf" });
  });

  it.each([
    ["File name", "filename"],
    ["Supplier", "supplier"],
    ["Invoice number", "invoice_number"],
  ])("debounces the %s box", async (label, key) => {
    const api = stubDashboard();
    await show();

    typeInto(label, "a");
    await advance(200);
    typeInto(label, "ac");
    await advance(299);

    // The box answers at once, while the URL and the API wait.
    expect(screen.getByRole("textbox", { name: label })).toHaveValue("ac");
    expect(navigation.router.replace).not.toHaveBeenCalled();
    expect(api.calls(LIST)).toHaveLength(1);

    await advance(1);

    expect(navigation.params.get(key)).toBe("ac");
    expect(lastListQuery(api)).toMatchObject({ [key]: "ac" });
    expect(api.calls(LIST)).toHaveLength(2);
    expect(navigation.router.replace).toHaveBeenCalledTimes(1);
  });

  it("does not lose a filter when two text boxes are typed in within the delay", async () => {
    const api = stubDashboard();
    await show();

    typeInto("File name", "march");
    typeInto("Supplier", "acme");
    await advance(1000);

    expect(navigation.params.get("filename")).toBe("march");
    expect(navigation.params.get("supplier")).toBe("acme");
    expect(lastListQuery(api)).toMatchObject({ filename: "march", supplier: "acme" });
  });

  it("does not scroll to the top when the URL changes", async () => {
    stubDashboard();
    await show();

    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "failed" } });
    await flush();

    expect(navigation.router.replace).toHaveBeenCalledWith("/?status=failed", { scroll: false });
  });

  it("reads filters from the URL on load", async () => {
    const api = stubDashboard();
    await show(
      "/?status=failed&needs_review=true&file_type=csv&filename=march&supplier=acme&invoice_number=42&date_from=2026-01-01&date_to=2026-02-01&offset=25",
    );

    expect(api.calls(LIST)[0].query).toEqual({
      status: "failed",
      needs_review: "true",
      file_type: "csv",
      filename: "march",
      supplier: "acme",
      invoice_number: "42",
      date_from: "2026-01-01",
      date_to: "2026-02-01",
      limit: "25",
      offset: "25",
    });
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("failed");
    expect(screen.getByRole("combobox", { name: "Review" })).toHaveValue("true");
    expect(screen.getByRole("combobox", { name: "File type" })).toHaveValue("csv");
    expect(screen.getByRole("textbox", { name: "File name" })).toHaveValue("march");
    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("acme");
    expect(screen.getByRole("textbox", { name: "Invoice number" })).toHaveValue("42");
    expect(screen.getByLabelText("Invoice date from")).toHaveValue("2026-01-01");
    expect(screen.getByLabelText("Invoice date to")).toHaveValue("2026-02-01");
  });

  it("follows the URL when it changes from outside", async () => {
    const api = stubDashboard();
    await show("/?supplier=acme");

    act(() => navigation.goTo("/?supplier=globex"));
    await flush();

    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("globex");
    expect(lastListQuery(api)).toMatchObject({ supplier: "globex" });
  });

  it("treats a nonsense offset as the first page", async () => {
    const api = stubDashboard();
    await show("/?offset=banana");
    expect(api.calls(LIST)[0].query.offset).toBe("0");
  });

  it("treats a negative offset as the first page", async () => {
    const api = stubDashboard();
    await show("/?offset=-25");
    expect(api.calls(LIST)[0].query.offset).toBe("0");
  });

  it("offers Clear filters only when a filter is set", async () => {
    stubDashboard();
    await show();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "failed" } });
    await flush();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("Clear filters clears every filter at once, and the page too", async () => {
    const api = stubDashboard();
    await show("/?status=failed&supplier=acme&date_from=2026-01-01&offset=25");

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await advance(1000);

    expect(navigation.path).toBe("/");
    expect(lastListQuery(api)).toEqual({ limit: "25", offset: "0" });
    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("");
  });

  // BUG, components/Dashboard.tsx:182-186: a real router.replace takes a round trip. A letter typed
  // after the debounce fired but before the URL changed is overwritten when the URL catches up.
  it("keeps what was typed while the URL was catching up", async () => {
    stubDashboard();
    await show();
    navigation.hold();

    typeInto("Supplier", "ac");
    await advance(300);
    typeInto("Supplier", "acm");
    act(() => navigation.release());
    await flush();

    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("acm");
  });
});

describe("Dashboard: polling", () => {
  it("polls every 3 seconds with a busy document", async () => {
    const api = stubDashboard([makeSummary({ status: "processing" })]);
    await show();
    expect(api.calls(LIST)).toHaveLength(1);

    await advance(2999);
    expect(api.calls(LIST)).toHaveLength(1);
    await advance(1);
    expect(api.calls(LIST)).toHaveLength(2);
    await advance(3000);
    expect(api.calls(LIST)).toHaveLength(3);
  });

  it("treats a queued document as busy", async () => {
    const api = stubDashboard([makeSummary({ status: "queued" })]);
    await show();

    await advance(3000);

    expect(api.calls(LIST)).toHaveLength(2);
  });

  it("polls every 15 seconds when nothing is busy", async () => {
    const api = stubDashboard([makeSummary({ status: "completed" })]);
    await show();

    await advance(14999);
    expect(api.calls(LIST)).toHaveLength(1);
    await advance(1);
    expect(api.calls(LIST)).toHaveLength(2);
  });

  it("slows down once the last document finishes", async () => {
    const api = stubDashboard([makeSummary({ status: "processing" })]);
    await show();

    api.routes[LIST] = makeList([makeSummary({ status: "completed" })]);
    await advance(3000);
    expect(api.calls(LIST)).toHaveLength(2);
    expect(screen.getByRole("table")).toHaveTextContent("Completed");

    await advance(14999);
    expect(api.calls(LIST)).toHaveLength(2);
    await advance(1);
    expect(api.calls(LIST)).toHaveLength(3);
  });

  it("stops polling after leaving the page", async () => {
    const api = stubDashboard();
    navigation.reset("/");
    const { unmount } = render(<Dashboard />);
    await flush();

    unmount();
    await advance(60000);

    expect(api.calls(LIST)).toHaveLength(1);
    expect(api.calls(METRICS)).toHaveLength(1);
  });
});

describe("Dashboard: pagination", () => {
  it("is hidden with exactly 25 results", async () => {
    stubApi({ [LIST]: makeList([makeSummary()], { total: 25 }), [METRICS]: NO_FAILURES });
    await show();

    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
  });

  it("appears with 26, showing the range, with Previous disabled", async () => {
    stubApi({ [LIST]: pagesOf(26), [METRICS]: NO_FAILURES });
    await show();

    const pages = within(screen.getByRole("navigation", { name: "Pages" }));
    expect(pages.getByText("1–25 of 26")).toBeInTheDocument();
    expect(pages.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(pages.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("Next sets the offset in the URL and fetches that page, without scrolling", async () => {
    const api = stubApi({ [LIST]: pagesOf(60), [METRICS]: NO_FAILURES });
    await show("/?status=failed");

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await flush();

    expect(navigation.router.replace).toHaveBeenLastCalledWith("/?status=failed&offset=25", {
      scroll: false,
    });
    expect(lastListQuery(api)).toMatchObject({ offset: "25", status: "failed" });
    expect(screen.getByRole("link", { name: "from-25.pdf" })).toBeInTheDocument();
    expect(screen.getByText("26–50 of 60")).toBeInTheDocument();
  });

  it("disables Next on the last page", async () => {
    stubApi({ [LIST]: pagesOf(60), [METRICS]: NO_FAILURES });
    await show("/?offset=50");

    expect(screen.getByText("51–60 of 60")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("Previous to the first page removes the offset from the URL", async () => {
    const api = stubApi({ [LIST]: pagesOf(60), [METRICS]: NO_FAILURES });
    await show("/?offset=25");

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await flush();

    expect(navigation.path).toBe("/");
    expect(lastListQuery(api)).toMatchObject({ offset: "0" });
  });

  it("changing a filter goes back to the first page", async () => {
    const api = stubApi({ [LIST]: pagesOf(60), [METRICS]: NO_FAILURES });
    await show("/?offset=25");

    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "failed" } });
    await flush();

    expect(navigation.params.get("offset")).toBeNull();
    expect(lastListQuery(api)).toMatchObject({ offset: "0", status: "failed" });
  });

  it("a debounced filter goes back to the first page too", async () => {
    const api = stubApi({ [LIST]: pagesOf(60), [METRICS]: NO_FAILURES });
    await show("/?offset=25");

    typeInto("Supplier", "acme");
    await advance(300);

    expect(navigation.params.get("offset")).toBeNull();
    expect(lastListQuery(api)).toMatchObject({ offset: "0", supplier: "acme" });
  });
});

describe("Dashboard: retry failed", () => {
  const withFailures = makeMetrics({ documents_by_status: { failed: 3, completed: 1 } });

  it("is hidden with no failures", async () => {
    stubDashboard([], NO_FAILURES);
    await show();

    expect(screen.queryByRole("button", { name: /^Retry/ })).not.toBeInTheDocument();
  });

  it("is hidden before the metrics load", async () => {
    stubApi({ [LIST]: makeList([]), [METRICS]: () => new Promise(() => undefined) });
    await show();

    expect(screen.queryByRole("button", { name: /^Retry/ })).not.toBeInTheDocument();
  });

  it("shows the count, calls the API, shows its result, and refreshes", async () => {
    const api = stubDashboard([], withFailures);
    api.routes[RETRY] = { queued: 3, failed_to_queue: 0 };
    await show();
    const lists = api.calls(LIST).length;
    const metrics = api.calls(METRICS).length;

    fireEvent.click(screen.getByRole("button", { name: "Retry 3 failed" }));
    await flush();

    expect(api.calls(RETRY)).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Queued 3 for another attempt.");
    expect(api.calls(LIST).length).toBeGreaterThan(lists);
    expect(api.calls(METRICS).length).toBeGreaterThan(metrics);
  });

  it("is disabled while it works", async () => {
    const answer = Promise.withResolvers<unknown>();
    const api = stubDashboard([], withFailures);
    api.routes[RETRY] = () => answer.promise;
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Retry 3 failed" }));
    await flush();

    expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();

    await act(async () => answer.resolve({ queued: 3, failed_to_queue: 0 }));
    expect(screen.getByRole("button", { name: "Retry 3 failed" })).toBeEnabled();
  });

  it("says when some could not be queued", async () => {
    const api = stubDashboard([], withFailures);
    api.routes[RETRY] = { queued: 2, failed_to_queue: 1 };
    await show();

    fireEvent.click(screen.getByRole("button", { name: "Retry 3 failed" }));
    await flush();

    expect(screen.getByRole("status")).toHaveTextContent(
      "Queued 2. 1 could not be queued, so try again shortly.",
    );
  });

  it("shows the API's message when the retry fails", async () => {
    const api = stubDashboard([], withFailures);
    api.routes[RETRY] = errorResponse("The queue is down.", 503);
    await show();
    const lists = api.calls(LIST).length;

    fireEvent.click(screen.getByRole("button", { name: "Retry 3 failed" }));
    await flush();

    expect(screen.getByRole("status")).toHaveTextContent("The queue is down.");
    expect(screen.getByRole("button", { name: "Retry 3 failed" })).toBeEnabled();
    expect(api.calls(LIST)).toHaveLength(lists);
  });

  it("keeps the result on show after the failures have gone", async () => {
    const api = stubDashboard([], withFailures);
    api.routes[RETRY] = { queued: 3, failed_to_queue: 0 };
    await show();

    api.routes[METRICS] = NO_FAILURES;
    fireEvent.click(screen.getByRole("button", { name: "Retry 3 failed" }));
    await flush();

    expect(screen.getByRole("status")).toHaveTextContent("Queued 3 for another attempt.");
    expect(screen.queryByRole("button", { name: /^Retry/ })).not.toBeInTheDocument();
  });
});

describe("Dashboard: uploading", () => {
  it("refreshes the list and metrics as soon as an upload is accepted", async () => {
    stubXhr();
    const api = stubDashboard();
    await show();
    const lists = api.calls(LIST).length;
    const metrics = api.calls(METRICS).length;

    fireEvent.change(screen.getByLabelText("Choose files to upload"), {
      target: { files: [makeFile("new.pdf")] },
    });
    await flush();
    FakeXhr.instances[0].respond(200, { results: [makeOutcome()] });
    await flush();

    expect(api.calls(LIST)).toHaveLength(lists + 1);
    expect(api.calls(METRICS)).toHaveLength(metrics + 1);
  });
});
