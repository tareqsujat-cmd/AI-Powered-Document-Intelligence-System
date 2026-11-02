import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DocumentDetail } from "@/lib/api";
import { PagePreview } from "./PagePreview";
import { DOC_ID, errorResponse, makeDetail, makePage, stubApi } from "@/test/fixtures";

const SHEET_ROUTE = `GET /documents/${DOC_ID}/pages/1`;

function show(document: DocumentDetail, page = 1) {
  const onPageChange = vi.fn();
  const view = render(<PagePreview document={document} page={page} onPageChange={onPageChange} />);
  return { onPageChange, ...view };
}

function sheetDocument(overrides: Partial<DocumentDetail> = {}) {
  return makeDetail({ file_type: "xlsx", filename: "book.xlsx", page_count: 2, ...overrides });
}

const previous = () => screen.getByRole("button", { name: "Previous" });
const next = () => screen.getByRole("button", { name: "Next" });

describe("PagePreview: pages of a PDF or image", () => {
  it("shows the image of the current page, described for a screen reader", () => {
    stubApi();
    show(makeDetail({ filename: "invoice.pdf" }), 2);

    const image = screen.getByRole("img", { name: "Page 2 of invoice.pdf" });
    expect(image.getAttribute("src")).toMatch(new RegExp(`/documents/${DOC_ID}/pages/2/image$`));
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
  });

  it("links to the original file in a new tab", () => {
    stubApi();
    show(makeDetail());

    const link = screen.getByRole("link", { name: "Open original" });
    expect(link.getAttribute("href")).toMatch(new RegExp(`/documents/${DOC_ID}/file$`));
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("disables Previous on the first page, while Next goes on", () => {
    stubApi();
    const { onPageChange } = show(makeDetail(), 1);

    expect(previous()).toBeDisabled();
    expect(next()).toBeEnabled();

    fireEvent.click(next());
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it("disables Next on the last page, while Previous goes back", () => {
    stubApi();
    const { onPageChange } = show(makeDetail(), 3);

    expect(next()).toBeDisabled();
    expect(previous()).toBeEnabled();

    fireEvent.click(previous());
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it("enables both in the middle", () => {
    stubApi();
    show(makeDetail(), 2);

    expect(previous()).toBeEnabled();
    expect(next()).toBeEnabled();
  });

  it("uses the stored page count before the length is known", () => {
    stubApi();
    show(makeDetail({ page_count: null, stored_page_count: 5 }));

    expect(screen.getByText("Page 1 of 5")).toBeInTheDocument();
  });

  it("still allows page 1 when no pages are known yet", () => {
    stubApi();
    show(makeDetail({ page_count: null, stored_page_count: 0 }));

    expect(screen.getByText("Page 1 of 1")).toBeInTheDocument();
    expect(previous()).toBeDisabled();
    expect(next()).toBeDisabled();
  });

  it("says so when the page image cannot be loaded", () => {
    stubApi();
    show(makeDetail());

    fireEvent.error(screen.getByRole("img"));

    expect(screen.getByText("This page has no preview.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("tries the image again on another page after one fails", () => {
    stubApi();
    const document = makeDetail();
    const { rerender } = show(document, 1);
    fireEvent.error(screen.getByRole("img"));

    rerender(<PagePreview document={document} page={2} onPageChange={vi.fn()} />);

    expect(screen.getByRole("img", { name: /Page 2/ })).toBeInTheDocument();
    expect(screen.queryByText("This page has no preview.")).not.toBeInTheDocument();
  });

  it("fetches no sheet text for a PDF", () => {
    const api = stubApi();
    show(makeDetail());

    expect(api.fetch).not.toHaveBeenCalled();
  });
});

describe("PagePreview: sheets", () => {
  it.each(["xlsx", "xls", "csv"] as const)("renders a %s page as a table", async (fileType) => {
    stubApi({
      [SHEET_ROUTE]: makePage({ text: "Sheet: Q1 sales\nItem | Qty\nPens | 2\nInk | 10" }),
    });
    show(sheetDocument({ file_type: fileType }));

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual(
      [
        ["Item", "Qty"],
        ["Pens", "2"],
        ["Ink", "10"],
      ],
    );
    expect(screen.getByText("Q1 sales")).toBeInTheDocument();
    expect(screen.getByText("Sheet 1 of 2")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("renders a sheet with no title", async () => {
    stubApi({ [SHEET_ROUTE]: makePage({ text: "Item | Qty" }) });
    show(sheetDocument());

    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
      "Item",
      "Qty",
    ]);
  });

  it("shows loading until the sheet arrives", async () => {
    stubApi({ [SHEET_ROUTE]: makePage({ text: "A | B" }) });
    show(sheetDocument());

    expect(screen.getByText("Loading sheet…")).toBeInTheDocument();
    await screen.findByRole("table");
    expect(screen.queryByText("Loading sheet…")).not.toBeInTheDocument();
  });

  it("shows the API's message when the sheet cannot be loaded", async () => {
    stubApi({ [SHEET_ROUTE]: errorResponse("Page not found.", 404) });
    show(sheetDocument());

    expect(await screen.findByRole("alert")).toHaveTextContent("Page not found.");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it.each([
    ["no text", ""],
    ["only blank lines", "\n  \n"],
    ["only a title", "Sheet: Empty"],
  ])("says a sheet with %s is empty", async (_name, text) => {
    stubApi({ [SHEET_ROUTE]: makePage({ text }) });
    show(sheetDocument());

    expect(await screen.findByText("This sheet is empty.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("loads the next sheet when the page changes, and aborts the old request", async () => {
    const api = stubApi({
      [SHEET_ROUTE]: () => new Promise(() => undefined),
      [`GET /documents/${DOC_ID}/pages/2`]: makePage({ page_number: 2, text: "Second | sheet" }),
    });
    const document = sheetDocument();
    const { rerender } = show(document, 1);
    const firstSignal = api.calls(SHEET_ROUTE)[0].init?.signal;

    rerender(<PagePreview document={document} page={2} onPageChange={vi.fn()} />);

    expect(await screen.findByText("Second")).toBeInTheDocument();
    expect(firstSignal?.aborted).toBe(true);
  });

  it("aborts the request when the preview goes away", async () => {
    const api = stubApi({ [SHEET_ROUTE]: () => new Promise(() => undefined) });
    const { unmount } = show(sheetDocument());
    const signal = api.calls(SHEET_ROUTE)[0].init?.signal;

    unmount();

    await waitFor(() => expect(signal?.aborted).toBe(true));
  });
});
