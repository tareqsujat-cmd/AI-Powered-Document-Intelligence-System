import { describe, expect, it } from "vitest";
import {
  confidenceLevel,
  eventLabel,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  invoiceTabLabel,
  isBusy,
  openIssueCount,
  pageRangeLabel,
  parseSheetText,
  processingSeconds,
  stageLabel,
} from "./format";
import { makeFields, makeIssue, makeResult } from "@/test/fixtures";

const KB = 1024;
const MB = 1024 * 1024;

describe("stageLabel and eventLabel", () => {
  it.each([
    ["upload", "Waiting"],
    ["file_validation", "Checking the file"],
    ["text_extraction", "Reading text"],
    ["llm_extraction", "Extracting invoice data"],
    ["validation", "Validating"],
    ["indexing", "Indexing for search"],
    ["finished", "Finished"],
  ])("names the stage %s", (stage, label) => {
    expect(stageLabel(stage)).toBe(label);
  });

  it("shows an unknown stage as it is rather than nothing", () => {
    expect(stageLabel("new_stage")).toBe("new_stage");
  });

  it.each([
    ["uploaded", "Uploaded"],
    ["stage_started", "Started"],
    ["stage_completed", "Finished"],
    ["stage_failed", "Failed"],
    ["retry_scheduled", "Retry scheduled"],
    ["reprocess_requested", "Reprocess requested"],
    ["manual_correction", "Reviewed by a person"],
  ])("names the event %s", (event, label) => {
    expect(eventLabel(event)).toBe(label);
  });

  it("shows an unknown event as it is rather than nothing", () => {
    expect(eventLabel("new_event")).toBe("new_event");
  });
});

describe("isBusy", () => {
  it.each([
    ["queued", true],
    ["processing", true],
    ["completed", false],
    ["failed", false],
  ] as const)("%s is busy: %s", (status, busy) => {
    expect(isBusy(status)).toBe(busy);
  });
});

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [1, "1 B"],
    [KB - 1, "1023 B"],
    [KB, "1 KB"],
    [KB * 1.5, "2 KB"],
    [KB * 500, "500 KB"],
    [MB, "1.0 MB"],
    [MB * 1.5, "1.5 MB"],
    [MB * 1024, "1024.0 MB"],
  ])("%i bytes reads as %s", (bytes, label) => {
    expect(formatBytes(bytes)).toBe(label);
  });

  // BUG (cosmetic), lib/format.ts:64: a size just under 1 MB rounds up to "1024 KB".
  it("does not show 1024 KB just below the megabyte boundary", () => {
    expect(formatBytes(MB - 1)).toBe("1.0 MB");
  });
});

describe("formatDateTime", () => {
  it.each([[null], [""], ["not a date"]])("shows a dash for %j", (value) => {
    expect(formatDateTime(value)).toBe("—");
  });

  it("shows the year, month and day of a valid date", () => {
    const text = formatDateTime("2026-03-15T12:00:00Z");
    expect(text).toMatch(/2026/);
    expect(text).toMatch(/15/);
    expect(text).toMatch(/Mar/);
  });
});

describe("formatPercent", () => {
  it.each([
    [null, "—"],
    [0, "0%"],
    [0.856, "86%"],
    [1, "100%"],
  ])("%s reads as %s", (value, label) => {
    expect(formatPercent(value)).toBe(label);
  });
});

describe("formatDuration", () => {
  it.each([
    [null, "—"],
    [0, "0.0 s"],
    [12.34, "12.3 s"],
    [59.9, "59.9 s"],
    [60, "1.0 min"],
    [90, "1.5 min"],
  ])("%s seconds reads as %s", (value, label) => {
    expect(formatDuration(value)).toBe(label);
  });
});

describe("processingSeconds", () => {
  const start = "2026-03-15T12:00:00Z";

  it("is the time between start and finish", () => {
    expect(processingSeconds(start, "2026-03-15T12:01:30Z")).toBe(90);
  });

  it("is zero when both are the same moment", () => {
    expect(processingSeconds(start, start)).toBe(0);
  });

  it.each([
    [null, start],
    [start, null],
    [null, null],
  ])("is null with a missing time (%s, %s)", (started, finished) => {
    expect(processingSeconds(started, finished)).toBeNull();
  });

  it("is null when the times are reversed", () => {
    expect(processingSeconds("2026-03-15T12:01:00Z", start)).toBeNull();
  });

  it("is null when a time cannot be read", () => {
    expect(processingSeconds("garbage", start)).toBeNull();
  });
});

describe("confidenceLevel", () => {
  it.each([
    [1, "high"],
    [0.85, "high"],
    [0.8499, "medium"],
    [0.6, "medium"],
    [0.5999, "low"],
    [0, "low"],
  ])("%f is %s", (confidence, level) => {
    expect(confidenceLevel(confidence)).toBe(level);
  });
});

describe("openIssueCount", () => {
  it("counts unresolved errors and warnings, but not info or resolved issues", () => {
    const result = makeResult({
      validation_errors: [
        makeIssue({ id: "1", severity: "error" }),
        makeIssue({ id: "2", severity: "warning" }),
        makeIssue({ id: "3", severity: "info" }),
        makeIssue({ id: "4", severity: "error", is_resolved: true }),
      ],
    });
    expect(openIssueCount(result)).toBe(2);
  });

  it("is zero with no issues", () => {
    expect(openIssueCount(makeResult())).toBe(0);
  });
});

describe("invoiceTabLabel", () => {
  it("uses the invoice number", () => {
    expect(invoiceTabLabel(makeResult())).toBe("INV-001");
  });

  it("counts from one when the number is unknown", () => {
    const result = makeResult({
      invoice_index: 1,
      fields: makeFields({ invoice_number: { value: null } }),
    });
    expect(invoiceTabLabel(result)).toBe("Invoice 2");
  });
});

describe("pageRangeLabel", () => {
  it.each([
    [null, null, ""],
    [null, 5, ""],
    [3, null, "Page 3"],
    [3, 3, "Page 3"],
    [3, 5, "Pages 3–5"],
  ])("(%s, %s) reads as %j", (start, end, label) => {
    expect(pageRangeLabel(start, end)).toBe(label);
  });
});

describe("parseSheetText", () => {
  it("reads the title from a Sheet: line and splits the cells", () => {
    expect(parseSheetText("Sheet: Q1\nItem | Qty\nPens | 2")).toEqual({
      title: "Q1",
      rows: [
        ["Item", "Qty"],
        ["Pens", "2"],
      ],
    });
  });

  it("treats the first line as a row when it has no Sheet: title", () => {
    expect(parseSheetText("Item | Qty\nPens | 2")).toEqual({
      title: null,
      rows: [
        ["Item", "Qty"],
        ["Pens", "2"],
      ],
    });
  });

  it("does not take a word that only starts like a title as one", () => {
    expect(parseSheetText("Sheets | 2").title).toBeNull();
  });

  it("skips blank and whitespace-only lines", () => {
    expect(parseSheetText("\nSheet: Q1\n\n   \nPens | 2\n\n")).toEqual({
      title: "Q1",
      rows: [["Pens", "2"]],
    });
  });

  it("gives no rows for empty text", () => {
    expect(parseSheetText("")).toEqual({ title: null, rows: [] });
  });

  it("gives a title and no rows for a sheet with only its name", () => {
    expect(parseSheetText("Sheet: Empty")).toEqual({ title: "Empty", rows: [] });
  });

  it("keeps a single-cell row whole", () => {
    expect(parseSheetText("Just one cell").rows).toEqual([["Just one cell"]]);
  });
});
