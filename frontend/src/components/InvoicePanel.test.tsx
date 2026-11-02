import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { InvoiceResult, ValidationIssue } from "@/lib/api";
import { InvoicePanel } from "./InvoicePanel";
import {
  type ApiCall,
  DOC_ID,
  errorResponse,
  makeField,
  makeFields,
  makeIssue,
  makeLineItem,
  makeResult,
  RESULT_ID,
  stubApi,
} from "@/test/fixtures";

const CORRECT_ROUTE = `PATCH /documents/${DOC_ID}/results/${RESULT_ID}`;
const reviewRoute = (id: string) => `PATCH /documents/${DOC_ID}/validation-errors/${id}`;

/** Holds the result as the document page does, so a saved change shows on screen. */
function Harness({
  initial,
  readOnly,
  onReviewChanged,
  onShowPage,
}: {
  initial: InvoiceResult;
  readOnly: boolean;
  onReviewChanged: () => void;
  onShowPage: (page: number) => void;
}) {
  const [result, setResult] = useState(initial);
  return (
    <InvoicePanel
      documentId={DOC_ID}
      result={result}
      readOnly={readOnly}
      onResultChanged={setResult}
      onReviewChanged={onReviewChanged}
      onShowPage={onShowPage}
    />
  );
}

function show(result: InvoiceResult = makeResult(), readOnly = false) {
  const onReviewChanged = vi.fn();
  const onShowPage = vi.fn();
  const view = render(
    <Harness
      initial={result}
      readOnly={readOnly}
      onReviewChanged={onReviewChanged}
      onShowPage={onShowPage}
    />,
  );
  return { onReviewChanged, onShowPage, ...view };
}

/** A PATCH that answers with the sent correction applied to the fields. */
function echoCorrections(base: InvoiceResult = makeResult()) {
  return stubApi({
    [CORRECT_ROUTE]: (call: ApiCall) => {
      const fields = { ...base.fields };
      for (const [name, value] of Object.entries(call.body as Record<string, string | null>)) {
        if (name in fields) {
          fields[name as keyof typeof fields] = makeField({ value, confidence: 1 });
        }
      }
      return { ...base, fields, version: base.version + 1 };
    },
  });
}

function fieldRow(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  return within(term.parentElement as HTMLElement);
}

function startEditing(label: string) {
  fireEvent.click(screen.getByRole("button", { name: `Edit ${label}` }));
}

function type(input: HTMLElement, value: string) {
  fireEvent.change(input, { target: { value } });
}

const issueMessages = () => screen.getAllByText(/^Issue /).map((node) => node.textContent);

describe("InvoicePanel: fields", () => {
  it("shows all 8 fields in order", () => {
    stubApi();
    show();

    expect(screen.getAllByRole("term").map((node) => node.textContent)).toEqual([
      "Invoice number",
      "Invoice date",
      "Supplier",
      "Customer",
      "Subtotal",
      "Tax",
      "Total",
      "Currency",
    ]);
  });

  it("shows a value with its confidence and source snippet", () => {
    stubApi();
    show(
      makeResult({
        fields: makeFields({
          total: { value: "120.00", confidence: 0.73, source_snippet: "Grand total 120.00" },
        }),
      }),
    );

    const row = fieldRow("Total");
    expect(row.getByText("120.00")).toBeInTheDocument();
    expect(row.getByText("73%")).toBeInTheDocument();
    expect(row.getByText("“Grand total 120.00”")).toBeInTheDocument();
  });

  it("shows an unknown value as Unknown with its reason and no confidence", () => {
    stubApi();
    show(
      makeResult({
        fields: makeFields({
          tax: { value: null, confidence: 0, reason: "No tax line in the document." },
        }),
      }),
    );

    const row = fieldRow("Tax");
    expect(row.getByText("Unknown")).toBeInTheDocument();
    expect(row.getByText("No tax line in the document.")).toBeInTheDocument();
    expect(row.queryByText("0%")).not.toBeInTheDocument();
  });

  it("calls onShowPage from a field's page link", () => {
    stubApi();
    const { onShowPage } = show(
      makeResult({ fields: makeFields({ supplier_name: { source_page: 4 } }) }),
    );

    fireEvent.click(fieldRow("Supplier").getByRole("button", { name: "Page 4" }));

    expect(onShowPage).toHaveBeenCalledWith(4);
  });

  it("has no page link when the page is unknown", () => {
    stubApi();
    show();

    expect(screen.queryByRole("button", { name: /^Page / })).not.toBeInTheDocument();
  });
});

describe("InvoicePanel: correcting a field", () => {
  it("opens an input holding the current value", () => {
    stubApi();
    show();

    startEditing("Supplier");

    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("Acme Ltd");
    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveFocus();
  });

  it("opens an empty input for an unknown value", () => {
    stubApi();
    show(makeResult({ fields: makeFields({ tax: { value: null } }) }));

    startEditing("Tax");

    expect(screen.getByRole("textbox", { name: "Tax" })).toHaveValue("");
  });

  it("uses a date input for the invoice date", () => {
    stubApi();
    show();

    startEditing("Invoice date");

    const input = screen.getByLabelText("Invoice date");
    expect(input).toHaveAttribute("type", "date");
    expect(input).toHaveValue("2026-03-01");
  });

  it("saves exactly that one field with PATCH, then shows the server's answer", async () => {
    const api = echoCorrections();
    const { onReviewChanged } = show();

    startEditing("Supplier");
    type(screen.getByRole("textbox", { name: "Supplier" }), "  New Supplier  ");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("New Supplier")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Supplier" })).not.toBeInTheDocument();
    const calls = api.calls(CORRECT_ROUTE);
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ supplier_name: "New Supplier" });
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it("saves when Enter submits the form", async () => {
    const api = echoCorrections();
    show();

    startEditing("Customer");
    const input = screen.getByRole("textbox", { name: "Customer" });
    type(input, "Initech");
    fireEvent.submit(input);

    expect(await screen.findByText("Initech")).toBeInTheDocument();
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ customer_name: "Initech" });
  });

  it.each([
    ["empty", ""],
    ["only spaces", "   "],
  ])("sends null for a value that is %s", async (_name, value) => {
    const api = echoCorrections();
    show();

    startEditing("Supplier");
    type(screen.getByRole("textbox", { name: "Supplier" }), value);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.calls(CORRECT_ROUTE)).toHaveLength(1));
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ supplier_name: null });
    expect(await fieldRow("Supplier").findByText("Unknown")).toBeInTheDocument();
  });

  it.each([
    ["Subtotal", "subtotal"],
    ["Tax", "tax"],
    ["Total", "total"],
  ])("strips thousands separators from %s", async (label, name) => {
    const api = echoCorrections();
    show();

    startEditing(label);
    type(screen.getByRole("textbox", { name: label }), "1,234,567.50");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.calls(CORRECT_ROUTE)).toHaveLength(1));
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ [name]: "1234567.50" });
    await fieldRow(label).findByText("1234567.50");
  });

  it("keeps commas in a field that is not money", async () => {
    const api = echoCorrections();
    show();

    startEditing("Supplier");
    type(screen.getByRole("textbox", { name: "Supplier" }), "Acme, Inc");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Acme, Inc")).toBeInTheDocument();
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ supplier_name: "Acme, Inc" });
  });

  it("uppercases the currency and limits it to 3 letters", async () => {
    const api = echoCorrections();
    show();

    startEditing("Currency");
    const input = screen.getByRole("textbox", { name: "Currency" });
    expect(input).toHaveAttribute("maxlength", "3");
    type(input, "eur");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("EUR")).toBeInTheDocument();
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ currency: "EUR" });
  });

  it("discards the change on Escape", () => {
    const api = stubApi();
    show();

    startEditing("Supplier");
    const input = screen.getByRole("textbox", { name: "Supplier" });
    type(input, "Typed by mistake");
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByRole("textbox", { name: "Supplier" })).not.toBeInTheDocument();
    expect(fieldRow("Supplier").getByText("Acme Ltd")).toBeInTheDocument();
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("discards the change on Cancel, and the next edit starts fresh", () => {
    const api = stubApi();
    show();

    startEditing("Supplier");
    type(screen.getByRole("textbox", { name: "Supplier" }), "Typed by mistake");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(fieldRow("Supplier").getByText("Acme Ltd")).toBeInTheDocument();
    expect(api.fetch).not.toHaveBeenCalled();

    startEditing("Supplier");
    expect(screen.getByRole("textbox", { name: "Supplier" })).toHaveValue("Acme Ltd");
  });

  it("stays in edit mode with the typed value and shows a server error", async () => {
    stubApi({ [CORRECT_ROUTE]: errorResponse("The total is too large.", 422) });
    const { onReviewChanged } = show();

    startEditing("Total");
    type(screen.getByRole("textbox", { name: "Total" }), "999999");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The total is too large.");
    expect(screen.getByRole("textbox", { name: "Total" })).toHaveValue("999999");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(onReviewChanged).not.toHaveBeenCalled();
  });

  it.each([
    ["Total", "1.2.3"],
    ["Total", "1.23456"],
    ["Tax", "12 USD"],
    ["Currency", "U1"],
    ["Currency", "US"],
  ])("refuses %s of %s, without calling the API", async (label, value) => {
    const api = stubApi();
    show();

    startEditing(label);
    type(screen.getByRole("textbox", { name: label }), value);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: label })).toHaveValue(value);
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("accepts a negative amount, as on a credit note", async () => {
    const api = echoCorrections();
    show();

    startEditing("Total");
    type(screen.getByRole("textbox", { name: "Total" }), "-120.50");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await fieldRow("Total").findByText("-120.50");
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ total: "-120.50" });
  });

  it("refuses an amount that is not a number, without calling the API", async () => {
    const api = stubApi();
    show();

    startEditing("Total");
    type(screen.getByRole("textbox", { name: "Total" }), "abc");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/as a number/);
    expect(screen.getByRole("textbox", { name: "Total" })).toHaveValue("abc");
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("clears the error when editing starts again", async () => {
    stubApi({ [CORRECT_ROUTE]: errorResponse("Not a valid decimal.", 422) });
    show();

    startEditing("Total");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    startEditing("Total");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("blocks a second save while one is running", async () => {
    const { promise, resolve } = Promise.withResolvers<InvoiceResult>();
    stubApi({ [CORRECT_ROUTE]: () => promise });
    show();

    startEditing("Supplier");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();

    await act(async () => resolve(makeResult()));
    expect(screen.queryByRole("button", { name: "Saving…" })).not.toBeInTheDocument();
  });
});

describe("InvoicePanel: read-only", () => {
  const withEverything = makeResult({
    line_items: [makeLineItem()],
    validation_errors: [makeIssue()],
  });

  it("hides every edit and review control", () => {
    stubApi();
    show(withEverything, true);

    expect(screen.queryByRole("button", { name: /^Edit/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).not.toBeInTheDocument();
    expect(screen.getByText("Acme Ltd")).toBeInTheDocument();
    expect(screen.getByText("Consulting")).toBeInTheDocument();
    expect(screen.getByText(withEverything.validation_errors[0].message)).toBeInTheDocument();
  });

  it("hides Reopen on a reviewed issue too", () => {
    stubApi();
    show(makeResult({ validation_errors: [makeIssue({ is_resolved: true })] }), true);

    expect(screen.queryByRole("button", { name: "Reopen" })).not.toBeInTheDocument();
  });

  it("still allows a page link", () => {
    stubApi();
    const { onShowPage } = show(
      makeResult({ fields: makeFields({ total: { source_page: 2 } }) }),
      true,
    );

    fireEvent.click(screen.getByRole("button", { name: "Page 2" }));

    expect(onShowPage).toHaveBeenCalledWith(2);
  });

  // BUG (minor), components/InvoicePanel.tsx:172: read-only only hides the Edit button, so a form
  // already open stays open with Save when the document starts processing from another tab.
  it("closes an open editor when the panel becomes read-only", () => {
    stubApi();
    const panel = (readOnly: boolean) => (
      <InvoicePanel
        documentId={DOC_ID}
        result={makeResult()}
        readOnly={readOnly}
        onResultChanged={vi.fn()}
        onReviewChanged={vi.fn()}
        onShowPage={vi.fn()}
      />
    );
    const { rerender } = render(panel(false));
    startEditing("Supplier");

    rerender(panel(true));

    expect(screen.queryByRole("textbox", { name: "Supplier" })).not.toBeInTheDocument();
  });
});

describe("InvoicePanel: line items", () => {
  const items = [
    makeLineItem({ id: "l1", description: "Consulting", quantity: "2", unit_price: "50.00", amount: "100.00" }),
    makeLineItem({ id: "l2", description: null, quantity: null, unit_price: null, amount: "20.00", source_page: 3 }),
  ];
  const result = makeResult({ line_items: items });
  const editLines = () => fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  const saveLines = () => fireEvent.click(screen.getByRole("button", { name: "Save line items" }));

  it("renders the rows, with unknown cells marked", () => {
    stubApi();
    show(result);

    expect(screen.getByRole("heading", { name: "Line items (2)" })).toBeInTheDocument();
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText("Consulting")).toBeInTheDocument();
    expect(within(rows[1]).getByText("100.00")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Unknown")).toBeInTheDocument();
    expect(within(rows[2]).getAllByText("—")).toHaveLength(2);
  });

  it("calls onShowPage from a line's page link", () => {
    stubApi();
    const { onShowPage } = show(result);

    fireEvent.click(screen.getByRole("button", { name: "Page 3" }));

    expect(onShowPage).toHaveBeenCalledWith(3);
  });

  it("says so when there are none", () => {
    stubApi();
    show(makeResult({ line_items: [] }));

    expect(screen.getByRole("heading", { name: "Line items (0)" })).toBeInTheDocument();
    expect(screen.getByText("No line items were found.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens inputs holding the current values, with blanks for unknowns", () => {
    stubApi();
    show(result);

    editLines();

    expect(screen.getByRole("textbox", { name: "Description, line 1" })).toHaveValue("Consulting");
    expect(screen.getByRole("textbox", { name: "Unit price, line 1" })).toHaveValue("50.00");
    expect(screen.getByRole("textbox", { name: "Description, line 2" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Qty, line 2" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Amount, line 2" })).toHaveValue("20.00");
  });

  it("adds a line, including from an empty list", () => {
    stubApi();
    show(makeResult({ line_items: [] }));

    editLines();
    expect(screen.queryByText("No line items were found.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add line" }));

    expect(screen.getByRole("textbox", { name: "Description, line 1" })).toHaveValue("");
    expect(screen.getByRole("heading", { name: "Line items (1)" })).toBeInTheDocument();
  });

  it("removes the chosen line and keeps what was typed in the others", () => {
    stubApi();
    show(result);

    editLines();
    type(screen.getByRole("textbox", { name: "Description, line 2" }), "Kept");
    fireEvent.click(screen.getByRole("button", { name: "Remove line 1" }));

    expect(screen.getByRole("heading", { name: "Line items (1)" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Description, line 1" })).toHaveValue("Kept");
    expect(screen.queryByRole("textbox", { name: "Description, line 2" })).not.toBeInTheDocument();
  });

  it("saves the whole list, blanks as null and without thousands separators", async () => {
    const saved = makeResult({ line_items: [makeLineItem({ id: "n1", description: "Saved" })] });
    const api = stubApi({ [CORRECT_ROUTE]: saved });
    const { onReviewChanged } = show(result);

    editLines();
    type(screen.getByRole("textbox", { name: "Amount, line 1" }), "1,100.00");
    fireEvent.click(screen.getByRole("button", { name: "Add line" }));
    type(screen.getByRole("textbox", { name: "Description, line 3" }), "  Extra  ");
    saveLines();

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Description, line 1" })).not.toBeInTheDocument();
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({
      line_items: [
        { description: "Consulting", quantity: "2", unit_price: "50.00", amount: "1100.00" },
        { description: null, quantity: null, unit_price: null, amount: "20.00" },
        { description: "Extra", quantity: null, unit_price: null, amount: null },
      ],
    });
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it("saves an empty list after every line is removed", async () => {
    const api = stubApi({ [CORRECT_ROUTE]: makeResult({ line_items: [] }) });
    show(makeResult({ line_items: [items[0]] }));

    editLines();
    fireEvent.click(screen.getByRole("button", { name: "Remove line 1" }));
    saveLines();

    expect(await screen.findByText("No line items were found.")).toBeInTheDocument();
    expect(api.calls(CORRECT_ROUTE)[0].body).toEqual({ line_items: [] });
  });

  it("discards everything on Cancel", () => {
    const api = stubApi();
    show(result);

    editLines();
    type(screen.getByRole("textbox", { name: "Description, line 1" }), "Changed");
    fireEvent.click(screen.getByRole("button", { name: "Add line" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("Consulting")).toBeInTheDocument();
    expect(screen.queryByText("Changed")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Line items (2)" })).toBeInTheDocument();
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("stays in edit mode and shows a server error", async () => {
    stubApi({ [CORRECT_ROUTE]: errorResponse("Too many line items.", 422) });
    show(result);

    editLines();
    type(screen.getByRole("textbox", { name: "Qty, line 1" }), "3");
    saveLines();

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many line items.");
    expect(screen.getByRole("textbox", { name: "Qty, line 1" })).toHaveValue("3");
  });

  it("refuses a quantity that is not a number, without calling the API", async () => {
    const api = stubApi();
    show(result);

    editLines();
    type(screen.getByRole("textbox", { name: "Qty, line 1" }), "two");
    saveLines();

    expect(await screen.findByRole("alert")).toHaveTextContent(/as numbers/);
    expect(screen.getByRole("textbox", { name: "Qty, line 1" })).toHaveValue("two");
    expect(api.fetch).not.toHaveBeenCalled();
  });
});

describe("InvoicePanel: validation issues", () => {
  function reviewAnswer(issue: ValidationIssue, isResolved: boolean) {
    return {
      validation_error: { ...issue, is_resolved: isResolved },
      needs_review: !isResolved,
    };
  }

  it("says all checks passed when there are none", () => {
    stubApi();
    show();

    expect(screen.getByText("All checks passed.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Validation" })).toBeInTheDocument();
  });

  it("shows each message and its severity", () => {
    stubApi();
    show(
      makeResult({
        validation_errors: [
          makeIssue({ id: "a", message: "Issue with a warning", severity: "warning" }),
        ],
      }),
    );

    expect(screen.getByText("Issue with a warning")).toBeInTheDocument();
    expect(screen.getByText("warning")).toBeInTheDocument();
    expect(screen.queryByText("All checks passed.")).not.toBeInTheDocument();
  });

  it("sorts unresolved before resolved, and errors before warnings before info", () => {
    stubApi();
    show(
      makeResult({
        validation_errors: [
          makeIssue({ id: "1", message: "Issue resolved error", severity: "error", is_resolved: true }),
          makeIssue({ id: "2", message: "Issue open info", severity: "info" }),
          makeIssue({ id: "3", message: "Issue open warning", severity: "warning" }),
          makeIssue({ id: "4", message: "Issue open error", severity: "error" }),
          makeIssue({ id: "5", message: "Issue resolved warning", severity: "warning", is_resolved: true }),
        ],
      }),
    );

    expect(issueMessages()).toEqual([
      "Issue open error",
      "Issue open warning",
      "Issue open info",
      "Issue resolved error",
      "Issue resolved warning",
    ]);
  });

  it("counts only the open errors and warnings as to review", () => {
    stubApi();
    show(
      makeResult({
        validation_errors: [
          makeIssue({ id: "1", severity: "error" }),
          makeIssue({ id: "2", severity: "warning" }),
          makeIssue({ id: "3", severity: "info" }),
          makeIssue({ id: "4", severity: "error", is_resolved: true }),
        ],
      }),
    );

    expect(screen.getByRole("heading", { name: "Validation (2 to review)" })).toBeInTheDocument();
  });

  it("marks an issue reviewed through the API, then lowers the count and moves it down", async () => {
    const first = makeIssue({ id: "first", message: "Issue first", severity: "error" });
    const second = makeIssue({ id: "second", message: "Issue second", severity: "warning" });
    const api = stubApi({ [reviewRoute("first")]: reviewAnswer(first, true) });
    const { onReviewChanged } = show(makeResult({ validation_errors: [first, second] }));

    const row = screen.getByText("Issue first").closest("li") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Mark reviewed" }));

    expect(await screen.findByText("Reviewed")).toBeInTheDocument();
    expect(api.calls(reviewRoute("first"))[0].body).toEqual({ is_resolved: true });
    expect(screen.getByRole("heading", { name: "Validation (1 to review)" })).toBeInTheDocument();
    expect(issueMessages()).toEqual(["Issue second", "Issue first"]);
    expect(onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it("reopens a reviewed issue through the API", async () => {
    const issue = makeIssue({ id: "first", is_resolved: true });
    const api = stubApi({ [reviewRoute("first")]: reviewAnswer(issue, false) });
    show(makeResult({ validation_errors: [issue] }));

    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));

    expect(await screen.findByRole("button", { name: "Mark reviewed" })).toBeInTheDocument();
    expect(api.calls(reviewRoute("first"))[0].body).toEqual({ is_resolved: false });
    expect(screen.queryByText("Reviewed")).not.toBeInTheDocument();
  });

  it("shows an error when a review cannot be saved, and leaves the issue open", async () => {
    stubApi({ [reviewRoute("issue-1")]: errorResponse("The document is being processed.", 409) });
    const { onReviewChanged } = show(makeResult({ validation_errors: [makeIssue()] }));

    fireEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The document is being processed.");
    expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeEnabled();
    expect(onReviewChanged).not.toHaveBeenCalled();
  });

  it("gives info issues no review button", () => {
    stubApi();
    show(makeResult({ validation_errors: [makeIssue({ severity: "info" })] }));

    expect(screen.queryByRole("button", { name: "Mark reviewed" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).not.toBeInTheDocument();
  });

  it("shows a cross-document match as verified", () => {
    stubApi();
    show(
      makeResult({
        validation_errors: [
          makeIssue({ code: "cross_document_verified", severity: "info", message: "Issue matches" }),
        ],
      }),
    );

    expect(screen.getByText("verified")).toBeInTheDocument();
    expect(screen.queryByText("info")).not.toBeInTheDocument();
  });

  it("links to a related document", () => {
    stubApi();
    show(makeResult({ validation_errors: [makeIssue({ related_document_id: "doc-77" })] }));

    expect(screen.getByRole("link", { name: "Open the related document" })).toHaveAttribute(
      "href",
      "/documents/doc-77",
    );
  });

  it("has no related link without a related document", () => {
    stubApi();
    show(makeResult({ validation_errors: [makeIssue()] }));

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  // BUG, components/InvoicePanel.tsx:75-81: onChanged spreads the `result` captured when the button
  // was pressed. Reviewing a second issue before the first answers restores the first as open.
  it("keeps both reviews when two issues are reviewed at the same time", async () => {
    const first = makeIssue({ id: "first", message: "Issue first" });
    const second = makeIssue({ id: "second", message: "Issue second" });
    const firstAnswer = Promise.withResolvers<unknown>();
    const secondAnswer = Promise.withResolvers<unknown>();
    stubApi({
      [reviewRoute("first")]: () => firstAnswer.promise,
      [reviewRoute("second")]: () => secondAnswer.promise,
    });
    show(makeResult({ validation_errors: [first, second] }));

    const [firstButton, secondButton] = screen.getAllByRole("button", { name: "Mark reviewed" });
    fireEvent.click(firstButton);
    fireEvent.click(secondButton);
    await act(async () => firstAnswer.resolve(reviewAnswer(first, true)));
    await act(async () => secondAnswer.resolve(reviewAnswer(second, true)));

    expect(screen.getAllByText("Reviewed")).toHaveLength(2);
  });
});

describe("InvoicePanel: extraction notes", () => {
  it("shows warnings when there are some", () => {
    stubApi();
    show(makeResult({ warnings: ["The scan is blurry."] }));

    expect(screen.getByRole("heading", { name: "Extraction notes" })).toBeInTheDocument();
    expect(screen.getByText("The scan is blurry.")).toBeInTheDocument();
  });

  it("hides the section with no warnings", () => {
    stubApi();
    show();

    expect(screen.queryByRole("heading", { name: "Extraction notes" })).not.toBeInTheDocument();
  });

  it("names the model and prompt for an LLM extraction", () => {
    stubApi();
    show(makeResult({ llm_model: "gemini-x", prompt_version: "v7", version: 3 }));

    expect(
      screen.getByText("Extracted by gemini-x, prompt version v7. Version 3."),
    ).toBeInTheDocument();
  });

  it("says when a spreadsheet was mapped with no LLM", () => {
    stubApi();
    show(makeResult({ method: "rule_based", llm_model: null, prompt_version: null }));

    expect(screen.getByText(/Mapped directly from the spreadsheet's columns, with no LLM\./)).toBeInTheDocument();
  });
});
