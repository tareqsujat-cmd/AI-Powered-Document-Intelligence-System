"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  correctResult,
  type Correction,
  type ExtractedField,
  type FieldName,
  type InvoiceResult,
  type LineItem,
  type LineItemCorrection,
  reviewIssue,
  type ValidationIssue,
} from "@/lib/api";
import { FIELD_LABELS, FIELD_ORDER, MONEY_FIELDS, SEVERITY_ORDER } from "@/lib/format";
import { Button, Card, ConfidenceMeter, ErrorNote, INPUT_CLASS, SeverityBadge } from "./ui";

function messageOf(problem: unknown, fallback: string): string {
  return problem instanceof Error ? problem.message : fallback;
}

export function InvoicePanel({
  documentId,
  result,
  readOnly,
  onResultChanged,
  onReviewChanged,
  onShowPage,
}: {
  documentId: string;
  result: InvoiceResult;
  readOnly: boolean;
  onResultChanged: (result: InvoiceResult) => void;
  onReviewChanged: () => void;
  onShowPage: (page: number) => void;
}) {
  // Two issues can be reviewed at once, so each answer must merge into the latest result, not the
  // one from when its button was pressed.
  const latest = useRef(result);
  useEffect(() => {
    latest.current = result;
  }, [result]);

  const save = async (correction: Correction) => {
    const updated = await correctResult(documentId, result.id, correction);
    onResultChanged(updated);
    onReviewChanged();
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <h2 className="border-b border-slate-200 px-4 py-2.5 text-sm font-semibold">
          Extracted fields
        </h2>
        <dl className="divide-y divide-slate-100">
          {FIELD_ORDER.map((name) => (
            <FieldRow
              key={name}
              name={name}
              field={result.fields[name]}
              readOnly={readOnly}
              onSave={(value) => save({ [name]: value })}
              onShowPage={onShowPage}
            />
          ))}
        </dl>
      </Card>

      <LineItems
        items={result.line_items}
        readOnly={readOnly}
        onSave={(line_items) => save({ line_items })}
        onShowPage={onShowPage}
      />

      <Issues
        documentId={documentId}
        issues={result.validation_errors}
        readOnly={readOnly}
        onChanged={(issue) => {
          latest.current = {
            ...latest.current,
            validation_errors: latest.current.validation_errors.map((current) =>
              current.id === issue.id ? issue : current,
            ),
          };
          onResultChanged(latest.current);
          onReviewChanged();
        }}
      />

      {result.warnings.length > 0 && (
        <Card className="px-4 py-3">
          <h2 className="text-sm font-semibold">Extraction notes</h2>
          <ul className="mt-1 list-disc pl-5 text-sm text-slate-700">
            {result.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Card>
      )}

      <p className="px-1 text-xs text-slate-500">
        {result.method === "llm"
          ? `Extracted by ${result.llm_model ?? "an LLM"}, prompt version ${result.prompt_version ?? "unknown"}.`
          : "Mapped directly from the spreadsheet's columns, with no LLM."}{" "}
        Version {result.version}.
      </p>
    </div>
  );
}

const AMOUNT = /^-?\d{1,14}(\.\d{1,4})?$/;

/** Plain guidance for the mistakes people make. The API still has the final say. */
function invalidValue(name: FieldName, value: string): string | null {
  if (MONEY_FIELDS.has(name) && !AMOUNT.test(value)) {
    return "Enter an amount as a number, such as 1100.50.";
  }
  if (name === "currency" && !/^[A-Z]{3}$/.test(value)) {
    return "Enter a three-letter currency code, such as USD.";
  }
  return null;
}

function FieldRow({
  name,
  field,
  readOnly,
  onSave,
  onShowPage,
}: {
  name: FieldName;
  field: ExtractedField;
  readOnly: boolean;
  onSave: (value: string | null) => Promise<void>;
  onShowPage: (page: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEditing = () => {
    setDraft(field.value ?? "");
    setError(null);
    setEditing(true);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    let value: string | null = draft.trim() || null;
    if (value !== null && name === "currency") value = value.toUpperCase();
    // Thousands separators are for reading, and the API takes a plain decimal.
    if (value !== null && MONEY_FIELDS.has(name)) value = value.replaceAll(",", "");
    const problem = value === null ? null : invalidValue(name, value);
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(value);
      setEditing(false);
    } catch (failure) {
      setError(messageOf(failure, "The correction could not be saved."));
    } finally {
      setSaving(false);
    }
  };

  const label = FIELD_LABELS[name];
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-x-3 px-4 py-2.5 text-sm">
      <dt className="pt-0.5 text-slate-500">{label}</dt>
      <dd className="min-w-0">
        {editing && !readOnly ? (
          <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-1.5">
            <div className="flex flex-wrap gap-1.5">
              <input
                autoFocus
                aria-label={label}
                type={name === "invoice_date" ? "date" : "text"}
                inputMode={MONEY_FIELDS.has(name) ? "decimal" : undefined}
                maxLength={name === "currency" ? 3 : undefined}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") setEditing(false);
                }}
                className={`${INPUT_CLASS} min-w-0 flex-1`}
              />
              <Button type="submit" variant="primary" disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
              <Button onClick={() => setEditing(false)} disabled={saving}>
                Cancel
              </Button>
            </div>
            <p className="text-xs text-slate-500">Leave it empty to mark this value as unknown.</p>
            {error && <ErrorNote>{error}</ErrorNote>}
          </form>
        ) : (
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 flex-col gap-0.5">
              {field.value !== null ? (
                <span className="break-words font-medium tabular-nums text-slate-900">
                  {field.value}
                </span>
              ) : (
                <span className="italic text-slate-400">Unknown</span>
              )}
              {field.reason && <span className="text-xs text-slate-500">{field.reason}</span>}
              {field.source_snippet && (
                <span className="truncate text-xs text-slate-500" title={field.source_snippet}>
                  “{field.source_snippet}”
                </span>
              )}
              <span className="flex items-center gap-2">
                {field.value !== null && <ConfidenceMeter confidence={field.confidence} />}
                {field.source_page !== null && (
                  <button
                    type="button"
                    onClick={() => onShowPage(field.source_page as number)}
                    className="text-xs text-indigo-700 hover:underline"
                  >
                    Page {field.source_page}
                  </button>
                )}
              </span>
            </div>
            {!readOnly && (
              <Button variant="ghost" onClick={startEditing} aria-label={`Edit ${label}`}>
                Edit
              </Button>
            )}
          </div>
        )}
      </dd>
    </div>
  );
}

const CELLS: { key: keyof LineItemCorrection; label: string; numeric: boolean }[] = [
  { key: "description", label: "Description", numeric: false },
  { key: "quantity", label: "Qty", numeric: true },
  { key: "unit_price", label: "Unit price", numeric: true },
  { key: "amount", label: "Amount", numeric: true },
];

type DraftRow = Record<keyof LineItemCorrection, string> & { key: number };

function LineItems({
  items,
  readOnly,
  onSave,
  onShowPage,
}: {
  items: LineItem[];
  readOnly: boolean;
  onSave: (items: LineItemCorrection[]) => Promise<void>;
  onShowPage: (page: number) => void;
}) {
  const [drafts, setDrafts] = useState<DraftRow[] | null>(null);
  const [nextKey, setNextKey] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEditing = () => {
    setDrafts(
      items.map((item, index) => ({
        key: index,
        description: item.description ?? "",
        quantity: item.quantity ?? "",
        unit_price: item.unit_price ?? "",
        amount: item.amount ?? "",
      })),
    );
    setNextKey(items.length);
    setError(null);
  };

  const setCell = (key: number, cell: keyof LineItemCorrection, value: string) => {
    setDrafts((rows) =>
      rows ? rows.map((row) => (row.key === key ? { ...row, [cell]: value } : row)) : rows,
    );
  };

  const save = async () => {
    if (!drafts) return;
    const clean = (value: string) => value.trim().replaceAll(",", "") || null;
    const badAmount = drafts.some((row) =>
      [row.quantity, row.unit_price, row.amount].some((cell) => {
        const value = clean(cell);
        return value !== null && !AMOUNT.test(value);
      }),
    );
    if (badAmount) {
      setError("Enter quantities and amounts as numbers, such as 1100.50.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(
        drafts.map((row) => ({
          description: row.description.trim() || null,
          quantity: clean(row.quantity),
          unit_price: clean(row.unit_price),
          amount: clean(row.amount),
        })),
      );
      setDrafts(null);
    } catch (problem) {
      setError(messageOf(problem, "The line items could not be saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
        <h2 className="text-sm font-semibold">Line items ({drafts?.length ?? items.length})</h2>
        {!readOnly && !drafts && (
          <Button variant="ghost" onClick={startEditing}>
            Edit
          </Button>
        )}
      </div>
      {!drafts && items.length === 0 && (
        <p className="px-4 py-4 text-sm text-slate-500">No line items were found.</p>
      )}
      {(drafts || items.length > 0) && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[28rem] text-left text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-xs text-slate-500">
                {CELLS.map((cell) => (
                  <th
                    key={cell.key}
                    scope="col"
                    className={`px-3 py-2 font-medium ${cell.numeric ? "text-right" : ""}`}
                  >
                    {cell.label}
                  </th>
                ))}
                {drafts && <th aria-label="Remove" className="w-16" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {drafts
                ? drafts.map((row, index) => (
                    <tr key={row.key}>
                      {CELLS.map((cell) => (
                        <td key={cell.key} className="px-1.5 py-1">
                          <input
                            aria-label={`${cell.label}, line ${index + 1}`}
                            value={row[cell.key]}
                            inputMode={cell.numeric ? "decimal" : undefined}
                            onChange={(event) => setCell(row.key, cell.key, event.target.value)}
                            className={`${INPUT_CLASS} w-full ${cell.numeric ? "text-right" : ""}`}
                          />
                        </td>
                      ))}
                      <td className="px-1 py-1 text-center">
                        <button
                          type="button"
                          aria-label={`Remove line ${index + 1}`}
                          onClick={() =>
                            setDrafts((rows) =>
                              rows ? rows.filter((other) => other.key !== row.key) : rows,
                            )
                          }
                          className="text-xs text-red-700 hover:underline"
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))
                : items.map((item) => (
                    <tr key={item.id} className="align-top">
                      <td className="px-3 py-2 text-slate-900">
                        {item.description ?? <span className="italic text-slate-400">Unknown</span>}
                        <span className="mt-0.5 flex items-center gap-2">
                          <ConfidenceMeter confidence={item.confidence} />
                          {item.source_page !== null && (
                            <button
                              type="button"
                              onClick={() => onShowPage(item.source_page as number)}
                              className="text-xs text-indigo-700 hover:underline"
                            >
                              Page {item.source_page}
                            </button>
                          )}
                        </span>
                      </td>
                      {[item.quantity, item.unit_price, item.amount].map((value, index) => (
                        <td
                          key={CELLS[index + 1].key}
                          className="px-3 py-2 text-right tabular-nums text-slate-800"
                        >
                          {value ?? "—"}
                        </td>
                      ))}
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>
      )}
      {drafts && (
        <div className="flex flex-col gap-2 border-t border-slate-100 px-4 py-3">
          {error && <ErrorNote>{error}</ErrorNote>}
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => {
                setDrafts((rows) => [
                  ...(rows ?? []),
                  { key: nextKey, description: "", quantity: "", unit_price: "", amount: "" },
                ]);
                setNextKey((key) => key + 1);
              }}
            >
              Add line
            </Button>
            <span className="flex-1" />
            <Button onClick={() => setDrafts(null)} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void save()} disabled={saving}>
              {saving ? "Saving…" : "Save line items"}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

function Issues({
  documentId,
  issues,
  readOnly,
  onChanged,
}: {
  documentId: string;
  issues: ValidationIssue[];
  readOnly: boolean;
  onChanged: (issue: ValidationIssue) => void;
}) {
  const ordered = [...issues].sort(
    (first, second) =>
      Number(first.is_resolved) - Number(second.is_resolved) ||
      SEVERITY_ORDER.indexOf(first.severity) - SEVERITY_ORDER.indexOf(second.severity),
  );
  const open = issues.filter((issue) => issue.severity !== "info" && !issue.is_resolved).length;
  return (
    <Card>
      <h2 className="border-b border-slate-200 px-4 py-2.5 text-sm font-semibold">
        Validation {open > 0 ? `(${open} to review)` : ""}
      </h2>
      {ordered.length === 0 ? (
        <p className="px-4 py-4 text-sm text-emerald-800">All checks passed.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {ordered.map((issue) => (
            <IssueRow
              key={issue.id}
              documentId={documentId}
              issue={issue}
              readOnly={readOnly}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function IssueRow({
  documentId,
  issue,
  readOnly,
  onChanged,
}: {
  documentId: string;
  issue: ValidationIssue;
  readOnly: boolean;
  onChanged: (issue: ValidationIssue) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = async () => {
    setSaving(true);
    setError(null);
    try {
      const response = await reviewIssue(documentId, issue.id, !issue.is_resolved);
      onChanged(response.validation_error);
    } catch (problem) {
      setError(messageOf(problem, "The issue could not be updated."));
    } finally {
      setSaving(false);
    }
  };

  const verified = issue.code === "cross_document_verified";
  const canReview = !readOnly && issue.severity !== "info";
  return (
    <li className={`flex flex-col gap-1 px-4 py-3 text-sm ${issue.is_resolved ? "opacity-60" : ""}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {verified ? (
              <span className="inline-flex rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                verified
              </span>
            ) : (
              <SeverityBadge severity={issue.severity} />
            )}
            {issue.is_resolved && (
              <span className="text-xs font-medium text-slate-600">Reviewed</span>
            )}
          </div>
          <p className="text-slate-800">{issue.message}</p>
          {issue.related_document_id && (
            <Link
              href={`/documents/${issue.related_document_id}`}
              className="text-xs text-indigo-700 hover:underline"
            >
              Open the related document
            </Link>
          )}
        </div>
        {canReview && (
          <Button onClick={() => void toggle()} disabled={saving} className="shrink-0">
            {issue.is_resolved ? "Reopen" : "Mark reviewed"}
          </Button>
        )}
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
    </li>
  );
}
