"use client";

import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { ApiError, uploadFile } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { ProgressBar } from "./ui";

const ACCEPT = ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv";
/** Parallel uploads, so that one huge file does not hold up the rest of a batch. */
const CONCURRENT_UPLOADS = 3;
const MAX_RATE_LIMIT_RETRIES = 5;
const DEFAULT_RETRY_SECONDS = 5;

type UploadState =
  | { phase: "waiting" }
  | { phase: "uploading"; percent: number }
  | { phase: "throttled"; seconds: number }
  | { phase: "accepted"; documentId: string }
  | { phase: "failed-on-record"; documentId: string; reason: string }
  | { phase: "rejected"; reason: string };

interface UploadRow {
  key: string;
  file: File;
  state: UploadState;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function UploadPanel({ onUploaded }: { onUploaded?: () => void }) {
  const [rows, setRows] = useState<UploadRow[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const nextKey = useRef(0);
  const queue = useRef<UploadRow[]>([]);
  const activeWorkers = useRef(0);

  const update = useCallback((key: string, state: UploadState) => {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, state } : row)));
  }, []);

  const uploadOne = useCallback(
    async (row: UploadRow) => {
      for (let attempt = 0; ; attempt += 1) {
        update(row.key, { phase: "uploading", percent: 0 });
        try {
          const outcome = await uploadFile(row.file, (percent) =>
            update(row.key, { phase: "uploading", percent }),
          );
          if (!outcome.accepted || !outcome.document) {
            update(row.key, {
              phase: "rejected",
              reason: outcome.error?.reason ?? "The file was not accepted.",
            });
          } else if (outcome.error) {
            update(row.key, {
              phase: "failed-on-record",
              documentId: outcome.document.id,
              reason: outcome.error.reason,
            });
          } else {
            update(row.key, { phase: "accepted", documentId: outcome.document.id });
          }
          onUploaded?.();
          return;
        } catch (problem) {
          // A large batch can reach the upload rate limit, which is no reason to lose the files.
          if (
            problem instanceof ApiError &&
            problem.status === 429 &&
            attempt < MAX_RATE_LIMIT_RETRIES
          ) {
            const seconds = problem.retryAfterSeconds ?? DEFAULT_RETRY_SECONDS;
            update(row.key, { phase: "throttled", seconds });
            await sleep(seconds * 1000);
            continue;
          }
          update(row.key, {
            phase: "rejected",
            reason: problem instanceof Error ? problem.message : "The upload failed.",
          });
          return;
        }
      }
    },
    [onUploaded, update],
  );

  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const added: UploadRow[] = Array.from(files).map((file) => {
        nextKey.current += 1;
        return { key: String(nextKey.current), file, state: { phase: "waiting" } };
      });
      if (added.length === 0) return;
      setRows((current) => [...added, ...current]);

      queue.current.push(...added);
      const worker = async () => {
        for (let row = queue.current.shift(); row; row = queue.current.shift()) {
          await uploadOne(row);
        }
        activeWorkers.current -= 1;
      };
      // One limit across batches, including files added while others are still uploading.
      const spare = Math.min(CONCURRENT_UPLOADS - activeWorkers.current, queue.current.length);
      for (let index = 0; index < spare; index += 1) {
        activeWorkers.current += 1;
        void worker();
      }
    },
    [uploadOne],
  );

  const inProgress = rows.some(
    (row) =>
      row.state.phase === "uploading" ||
      row.state.phase === "waiting" ||
      row.state.phase === "throttled",
  );

  return (
    <div className="flex flex-col gap-3">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          addFiles(event.dataTransfer.files);
        }}
        className={`flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-6 text-center transition-colors ${
          dragging ? "border-indigo-600 bg-indigo-50" : "border-slate-300 bg-white"
        }`}
      >
        <p className="text-sm text-slate-700">
          Drag invoices here, or{" "}
          <button
            type="button"
            onClick={() => input.current?.click()}
            className="font-medium text-indigo-700 underline underline-offset-2 hover:text-indigo-900"
          >
            choose files
          </button>
        </p>
        <p className="text-xs text-slate-500">PDF, JPG, PNG, TIFF, XLSX, XLS, or CSV</p>
        <input
          ref={input}
          type="file"
          multiple
          accept={ACCEPT}
          aria-label="Choose files to upload"
          className="sr-only"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      {rows.length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium text-slate-600">
              {inProgress ? "Uploading…" : "Uploads"}
            </p>
            {!inProgress && (
              <button
                type="button"
                onClick={() => setRows([])}
                className="text-xs text-slate-500 hover:text-slate-800"
              >
                Clear
              </button>
            )}
          </div>
          <ul className="flex max-h-56 flex-col divide-y divide-slate-100 overflow-y-auto rounded-md border border-slate-200 bg-white">
            {rows.map((row) => (
              <UploadRowView key={row.key} row={row} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function UploadRowView({ row }: { row: UploadRow }) {
  const { file, state } = row;
  return (
    <li className="flex flex-col gap-1 px-3 py-2 text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 truncate text-slate-800" title={file.name}>
          {file.name}
        </span>
        <span className="shrink-0 text-xs text-slate-500">{formatBytes(file.size)}</span>
      </div>
      {state.phase === "waiting" && <p className="text-xs text-slate-500">Waiting…</p>}
      {state.phase === "uploading" && (
        <ProgressBar percent={state.percent} label={`Uploading ${file.name}`} />
      )}
      {state.phase === "throttled" && (
        <p className="text-xs text-amber-800">
          Upload limit reached. Trying again in {state.seconds} s…
        </p>
      )}
      {state.phase === "accepted" && (
        <p className="text-xs text-emerald-800">
          Uploaded and queued.{" "}
          <Link className="underline" href={`/documents/${state.documentId}`}>
            View
          </Link>
        </p>
      )}
      {state.phase === "failed-on-record" && (
        <p className="text-xs text-red-800">
          {state.reason}{" "}
          <Link className="underline" href={`/documents/${state.documentId}`}>
            View
          </Link>
        </p>
      )}
      {state.phase === "rejected" && <p className="text-xs text-red-800">{state.reason}</p>}
    </li>
  );
}
