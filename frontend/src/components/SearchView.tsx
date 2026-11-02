"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useOptimistic, useState, useTransition } from "react";
import { search, type SearchHit, type SearchMode } from "@/lib/api";
import { SearchIcon } from "./icons";
import { Button, Card, EmptyState, ErrorNote, INPUT_CLASS } from "./ui";

const MODES: { value: SearchMode; label: string; hint: string }[] = [
  { value: "hybrid", label: "Hybrid", hint: "Both words and meaning" },
  { value: "keyword", label: "Keyword", hint: "Exact words and invoice numbers" },
  { value: "semantic", label: "Semantic", hint: "Similar meaning, even with different words" },
];

interface SearchOutcome {
  key: string;
  hits: SearchHit[] | null;
  error: string | null;
}

function readMode(value: string | null): SearchMode {
  return MODES.some((mode) => mode.value === value) ? (value as SearchMode) : "hybrid";
}

export function SearchView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const query = params.get("q") ?? "";
  const mode = readMode(params.get("mode"));
  // The radio button answers at once, rather than when the URL has finished changing.
  const [shownMode, showMode] = useOptimistic(mode);
  const [, startTransition] = useTransition();

  const [draft, setDraft] = useState(query);
  const [draftFor, setDraftFor] = useState(query);
  if (draftFor !== query) {
    // The URL changed, as with the browser's Back button, so the box follows it.
    setDraftFor(query);
    setDraft(query);
  }

  // An outcome belongs to one query, so results for an old query can never show under a new one.
  const [attempt, setAttempt] = useState(0);
  const searchKey = query ? `${mode}:${attempt}:${query}` : null;
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const current = outcome && outcome.key === searchKey ? outcome : null;
  const searching = searchKey !== null && current === null;
  const hits = current?.hits ?? null;
  const error = current?.error ?? null;

  useEffect(() => {
    if (searchKey === null) return undefined;
    const controller = new AbortController();
    search(query, mode, controller.signal)
      .then((body) => {
        if (!controller.signal.aborted) {
          setOutcome({ key: searchKey, hits: body.results, error: null });
        }
      })
      .catch((problem: unknown) => {
        if (controller.signal.aborted) return;
        const message = problem instanceof Error ? problem.message : "The search failed.";
        setOutcome({ key: searchKey, hits: null, error: message });
      });
    return () => controller.abort();
  }, [query, mode, searchKey]);

  const go = (nextQuery: string, nextMode: SearchMode) => {
    const next = new URLSearchParams();
    if (nextQuery) next.set("q", nextQuery);
    if (nextMode !== "hybrid") next.set("mode", nextMode);
    const text = next.toString();
    router.replace(text ? `${pathname}?${text}` : pathname);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    // The same words again would change nothing in the URL, but after an error they mean "retry".
    if (draft.trim() === query) setAttempt((count) => count + 1);
    go(draft.trim(), mode);
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Search</h1>
        <p className="text-sm text-slate-600">
          Find processed documents by invoice number, supplier, wording, or what they are about.
        </p>
      </div>

      <form role="search" onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex gap-2">
          <div className="relative min-w-0 flex-1">
            <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-5 -translate-y-1/2 text-slate-400" />
            <input
              type="search"
              aria-label="Search documents"
              placeholder="For example, INV-2026-0042 or cloud hosting"
              maxLength={500}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              className={`${INPUT_CLASS} w-full py-2.5 pl-10`}
            />
          </div>
          <Button type="submit" variant="primary" className="px-5">
            Search
          </Button>
        </div>
        <fieldset className="flex w-fit max-w-full flex-wrap gap-1 rounded-xl bg-slate-200/60 p-1">
          <legend className="sr-only">Search method</legend>
          {MODES.map((option) => (
            <label
              key={option.value}
              title={option.hint}
              className={`flex cursor-pointer items-center rounded-lg px-3 py-1.5 text-sm font-medium transition-colors has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-indigo-600 ${
                shownMode === option.value
                  ? "bg-white text-indigo-700 shadow-sm"
                  : "text-slate-600 hover:text-slate-900"
              }`}
            >
              <input
                type="radio"
                name="mode"
                value={option.value}
                checked={shownMode === option.value}
                onChange={() =>
                  startTransition(() => {
                    showMode(option.value);
                    go(draft.trim(), option.value);
                  })
                }
                className="sr-only"
              />
              {option.label}
            </label>
          ))}
        </fieldset>
        <p className="-mt-1 text-xs text-slate-500">
          {MODES.find((option) => option.value === shownMode)?.hint}
        </p>
      </form>

      {error && <ErrorNote>{error}</ErrorNote>}
      {searching && <p className="text-sm text-slate-500">Searching…</p>}
      {!searching && hits && hits.length === 0 && (
        <EmptyState icon={<SearchIcon className="size-6" />}>
          Nothing matched “{query}”.
        </EmptyState>
      )}
      {hits && hits.length > 0 && (
        <ul className="flex flex-col gap-2">
          {hits.map((hit) => (
            <SearchResult key={`${hit.document_id}-${hit.page_number}`} hit={hit} />
          ))}
        </ul>
      )}
    </div>
  );
}

function SearchResult({ hit }: { hit: SearchHit }) {
  const page = hit.page_number ?? 1;
  return (
    <li>
      <Card className="flex flex-col gap-1.5 px-4 py-3.5 text-sm transition-shadow hover:shadow-md hover:shadow-slate-900/5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <Link
            href={`/documents/${hit.document_id}?page=${page}`}
            className="min-w-0 truncate font-medium text-slate-900 hover:text-indigo-700 hover:underline"
          >
            {hit.filename}
          </Link>
          <span className="text-xs text-slate-500">
            {hit.page_number !== null && `Page ${hit.page_number} · `}
            {hit.matched_by.join(" and ")}
          </span>
        </div>
        <p className="line-clamp-3 whitespace-pre-line text-slate-600">{hit.snippet}</p>
      </Card>
    </li>
  );
}
