"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useOptimistic, useState, useTransition } from "react";
import { search, type SearchHit, type SearchMode } from "@/lib/api";
import { Button, Card, ErrorNote, INPUT_CLASS } from "./ui";

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
        <h1 className="text-xl font-semibold tracking-tight">Search</h1>
        <p className="text-sm text-slate-600">
          Find processed documents by invoice number, supplier, wording, or what they are about.
        </p>
      </div>

      <form role="search" onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex gap-2">
          <input
            type="search"
            aria-label="Search documents"
            placeholder="For example, INV-2026-0042 or cloud hosting"
            maxLength={500}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className={`${INPUT_CLASS} min-w-0 flex-1`}
          />
          <Button type="submit" variant="primary">
            Search
          </Button>
        </div>
        <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <legend className="sr-only">Search method</legend>
          {MODES.map((option) => (
            <label key={option.value} className="flex items-center gap-1.5 text-sm text-slate-700">
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
                className="accent-indigo-700"
              />
              {option.label}
              <span className="hidden text-xs text-slate-500 sm:inline">{option.hint}</span>
            </label>
          ))}
        </fieldset>
      </form>

      {error && <ErrorNote>{error}</ErrorNote>}
      {searching && <p className="text-sm text-slate-500">Searching…</p>}
      {!searching && hits && hits.length === 0 && (
        <Card className="px-4 py-8 text-center text-sm text-slate-500">
          Nothing matched “{query}”.
        </Card>
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
      <Card className="flex flex-col gap-1 px-4 py-3 text-sm">
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
