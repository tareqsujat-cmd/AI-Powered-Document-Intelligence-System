"use client";

import { useEffect, useState } from "react";
import {
  type DocumentDetail,
  fileUrl,
  getPage,
  type PageContent,
  pageImageUrl,
} from "@/lib/api";
import { parseSheetText } from "@/lib/format";
import { Button, ErrorNote } from "./ui";

const SPREADSHEETS = new Set(["xlsx", "xls", "csv"]);

export function PagePreview({
  document,
  page,
  onPageChange,
}: {
  document: DocumentDetail;
  page: number;
  onPageChange: (page: number) => void;
}) {
  const isSheet = SPREADSHEETS.has(document.file_type);
  // Before processing finishes a file's length may be unknown, which still allows page 1.
  const pageCount = Math.max(document.page_count ?? document.stored_page_count, 1);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
            Previous
          </Button>
          <span className="text-sm tabular-nums text-slate-600">
            {isSheet ? "Sheet" : "Page"} {page} of {pageCount}
          </span>
          <Button disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>
            Next
          </Button>
        </div>
        <a
          href={fileUrl(document.id)}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-indigo-700 hover:underline"
        >
          Open original
        </a>
      </div>
      {isSheet ? (
        <SheetPreview key={`${document.id}-${page}`} documentId={document.id} page={page} />
      ) : (
        <ImagePreview
          key={`${document.id}-${page}`}
          documentId={document.id}
          page={page}
          filename={document.filename}
        />
      )}
    </div>
  );
}

function ImagePreview({
  documentId,
  page,
  filename,
}: {
  documentId: string;
  page: number;
  filename: string;
}) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <p className="rounded-md border border-slate-200 bg-slate-50 px-4 py-10 text-center text-sm text-slate-500">
        This page has no preview.
      </p>
    );
  }
  return (
    // A plain img, because this is an API image with an unknown size, not a site asset.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={pageImageUrl(documentId, page)}
      alt={`Page ${page} of ${filename}`}
      onError={() => setFailed(true)}
      className="w-full rounded-md border border-slate-200 bg-white"
    />
  );
}

function SheetPreview({ documentId, page }: { documentId: string; page: number }) {
  const [content, setContent] = useState<PageContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getPage(documentId, page, controller.signal)
      .then(setContent)
      .catch((problem: unknown) => {
        if (!controller.signal.aborted) {
          setError(problem instanceof Error ? problem.message : "The sheet could not be loaded.");
        }
      });
    return () => controller.abort();
  }, [documentId, page]);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!content) return <p className="px-1 py-6 text-sm text-slate-500">Loading sheet…</p>;

  const { title, rows } = parseSheetText(content.text);
  if (rows.length === 0) {
    return <p className="px-1 py-6 text-sm text-slate-500">This sheet is empty.</p>;
  }
  return (
    <div className="max-h-[75vh] overflow-auto rounded-md border border-slate-200 bg-white">
      {title && (
        <p className="border-b border-slate-200 px-3 py-2 text-xs font-medium text-slate-600">
          {title}
        </p>
      )}
      <table className="w-full text-left text-xs">
        <tbody className="divide-y divide-slate-100">
          {rows.map((row, rowIndex) => (
            // Sheet rows have no identity of their own, and this table is never reordered.
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="whitespace-nowrap px-3 py-1.5 text-slate-700">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
