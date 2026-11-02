import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SearchView } from "./SearchView";
import {
  type ApiCall,
  errorResponse,
  FakeResponse,
  makeHit,
  navigation,
  stubApi,
} from "@/test/fixtures";

vi.mock("next/navigation", async () => (await import("@/test/fixtures")).navigationModule);

const ROUTE = "GET /search";

const box = () => screen.getByRole("searchbox", { name: "Search documents" });
const radio = (name: string) => screen.getByRole("radio", { name: new RegExp(`^${name}`) });

function submit(text: string) {
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

function hitsFor(...filenames: string[]) {
  return {
    results: filenames.map((filename, index) =>
      makeHit({ document_id: `doc-${index}`, filename }),
    ),
  };
}

beforeEach(() => {
  navigation.reset("/search");
});

describe("SearchView: before a search", () => {
  it("does not search until there is a query", () => {
    const api = stubApi();
    render(<SearchView />);

    expect(api.fetch).not.toHaveBeenCalled();
    expect(box()).toHaveValue("");
    expect(screen.queryByText("Searching…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Nothing matched/)).not.toBeInTheDocument();
  });

  it("does not search for an empty or blank submit", () => {
    const api = stubApi();
    render(<SearchView />);

    submit("   ");

    expect(api.fetch).not.toHaveBeenCalled();
    expect(navigation.path).toBe("/search");
  });
});

describe("SearchView: the URL", () => {
  it("submitting sets the URL and searches, trimmed", async () => {
    const api = stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);

    submit("  cloud hosting  ");

    expect(navigation.params.get("q")).toBe("cloud hosting");
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "found.pdf" })).toBeInTheDocument();
    expect(api.calls(ROUTE)).toHaveLength(1);
    expect(api.calls(ROUTE)[0].query).toMatchObject({ q: "cloud hosting", mode: "hybrid" });
    expect(screen.queryByText("Searching…")).not.toBeInTheDocument();
  });

  it("reads the query and mode from the URL on load", async () => {
    navigation.reset("/search?q=INV-42&mode=keyword");
    const api = stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);

    expect(box()).toHaveValue("INV-42");
    expect(radio("Keyword")).toBeChecked();

    await screen.findByRole("link", { name: "found.pdf" });
    expect(api.calls(ROUTE)[0].query).toMatchObject({ q: "INV-42", mode: "keyword" });
  });

  it("falls back to hybrid for an invalid mode", async () => {
    navigation.reset("/search?q=x&mode=telepathy");
    const api = stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);

    expect(radio("Hybrid")).toBeChecked();

    await screen.findByRole("link", { name: "found.pdf" });
    expect(api.calls(ROUTE)[0].query.mode).toBe("hybrid");
  });

  it("leaves the default mode out of the URL", async () => {
    stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);

    submit("cloud");
    await screen.findByRole("link", { name: "found.pdf" });

    expect(navigation.path).toBe("/search?q=cloud");
  });

  it("updates the box when the URL changes, and searches for it", async () => {
    navigation.reset("/search?q=first");
    const api = stubApi({
      [ROUTE]: (call: ApiCall) => hitsFor(`${call.query.q}.pdf`),
    });
    render(<SearchView />);
    await screen.findByRole("link", { name: "first.pdf" });

    act(() => navigation.goTo("/search?q=second"));

    expect(box()).toHaveValue("second");
    expect(await screen.findByRole("link", { name: "second.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "first.pdf" })).not.toBeInTheDocument();
    expect(api.calls(ROUTE)).toHaveLength(2);
  });

  it("clears the box and the results when the query leaves the URL", async () => {
    navigation.reset("/search?q=first");
    stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);
    await screen.findByRole("link", { name: "found.pdf" });

    act(() => navigation.goTo("/search"));

    expect(box()).toHaveValue("");
    expect(screen.queryByRole("link", { name: "found.pdf" })).not.toBeInTheDocument();
    expect(screen.queryByText("Searching…")).not.toBeInTheDocument();
  });
});

describe("SearchView: modes", () => {
  it("changing mode searches again, and puts the mode in the URL", async () => {
    navigation.reset("/search?q=cloud");
    const api = stubApi({ [ROUTE]: (call: ApiCall) => hitsFor(`${call.query.mode}.pdf`) });
    render(<SearchView />);
    await screen.findByRole("link", { name: "hybrid.pdf" });

    fireEvent.click(radio("Semantic"));

    expect(await screen.findByRole("link", { name: "semantic.pdf" })).toBeInTheDocument();
    expect(navigation.path).toBe("/search?q=cloud&mode=semantic");
    expect(radio("Semantic")).toBeChecked();
    expect(api.calls(ROUTE)).toHaveLength(2);
    expect(api.calls(ROUTE)[1].query).toMatchObject({ q: "cloud", mode: "semantic" });
  });

  it("removes the mode from the URL on going back to hybrid", async () => {
    navigation.reset("/search?q=cloud&mode=keyword");
    stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);
    await screen.findByRole("link", { name: "found.pdf" });

    fireEvent.click(radio("Hybrid"));
    await screen.findByRole("link", { name: "found.pdf" });

    expect(navigation.path).toBe("/search?q=cloud");
  });

  it("keeps the mode when a new query is submitted", async () => {
    navigation.reset("/search?mode=keyword");
    const api = stubApi({ [ROUTE]: hitsFor("found.pdf") });
    render(<SearchView />);

    submit("INV");
    await screen.findByRole("link", { name: "found.pdf" });

    expect(api.calls(ROUTE)[0].query).toMatchObject({ q: "INV", mode: "keyword" });
  });

  it("changing mode with nothing typed does not search", () => {
    const api = stubApi();
    render(<SearchView />);

    fireEvent.click(radio("Semantic"));

    expect(api.fetch).not.toHaveBeenCalled();
    expect(navigation.path).toBe("/search?mode=semantic");
  });
});

describe("SearchView: results", () => {
  it("links each result to its page in the document", async () => {
    navigation.reset("/search?q=cloud");
    stubApi({
      [ROUTE]: {
        results: [
          makeHit({
            document_id: "doc-7",
            filename: "march.pdf",
            page_number: 12,
            snippet: "Hosting for March",
            matched_by: ["keyword", "semantic"],
          }),
        ],
      },
    });
    render(<SearchView />);

    const link = await screen.findByRole("link", { name: "march.pdf" });
    expect(link).toHaveAttribute("href", "/documents/doc-7?page=12");
    expect(screen.getByText("Hosting for March")).toBeInTheDocument();
    expect(screen.getByText(/Page 12 · keyword and semantic/)).toBeInTheDocument();
  });

  it("links to the first page when a result has no page", async () => {
    navigation.reset("/search?q=cloud");
    stubApi({
      [ROUTE]: {
        results: [makeHit({ document_id: "doc-7", filename: "a.pdf", page_number: null })],
      },
    });
    render(<SearchView />);

    expect(await screen.findByRole("link", { name: "a.pdf" })).toHaveAttribute(
      "href",
      "/documents/doc-7?page=1",
    );
    expect(screen.queryByText(/Page /)).not.toBeInTheDocument();
  });

  it("says when nothing matched", async () => {
    navigation.reset("/search?q=zebra");
    stubApi({ [ROUTE]: { results: [] } });
    render(<SearchView />);

    expect(await screen.findByText("Nothing matched “zebra”.")).toBeInTheDocument();
  });

  it("shows an API error", async () => {
    navigation.reset("/search?q=cloud");
    stubApi({ [ROUTE]: errorResponse("Search is unavailable.", 503) });
    render(<SearchView />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Search is unavailable.");
    expect(screen.queryByText("Searching…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Nothing matched/)).not.toBeInTheDocument();
  });

  it("removes an error when a new search succeeds", async () => {
    navigation.reset("/search?q=bad");
    stubApi({
      [ROUTE]: (call: ApiCall) =>
        call.query.q === "bad" ? errorResponse("Broken.", 500) : hitsFor("found.pdf"),
    });
    render(<SearchView />);
    await screen.findByRole("alert");

    submit("good");

    expect(await screen.findByRole("link", { name: "found.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("hides the old results while a new search runs", async () => {
    navigation.reset("/search?q=first");
    const slow = Promise.withResolvers<unknown>();
    stubApi({
      [ROUTE]: (call: ApiCall) => (call.query.q === "first" ? hitsFor("first.pdf") : slow.promise),
    });
    render(<SearchView />);
    await screen.findByRole("link", { name: "first.pdf" });

    submit("second");

    expect(screen.queryByRole("link", { name: "first.pdf" })).not.toBeInTheDocument();
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    await act(async () => slow.resolve(hitsFor("second.pdf")));
    expect(screen.getByRole("link", { name: "second.pdf" })).toBeInTheDocument();
  });

  it("an old slow response never replaces a newer one, and is aborted", async () => {
    navigation.reset("/search?q=old");
    const old = Promise.withResolvers<unknown>();
    const api = stubApi({
      [ROUTE]: (call: ApiCall) => (call.query.q === "old" ? old.promise : hitsFor("new.pdf")),
    });
    render(<SearchView />);

    act(() => navigation.goTo("/search?q=new"));
    await screen.findByRole("link", { name: "new.pdf" });
    expect(api.calls(ROUTE)[0].init?.signal?.aborted).toBe(true);

    await act(async () => old.resolve(hitsFor("old.pdf")));

    expect(screen.getByRole("link", { name: "new.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "old.pdf" })).not.toBeInTheDocument();
  });

  it("an old failure never replaces a newer result", async () => {
    navigation.reset("/search?q=old");
    const old = Promise.withResolvers<unknown>();
    stubApi({
      [ROUTE]: (call: ApiCall) => (call.query.q === "old" ? old.promise : hitsFor("new.pdf")),
    });
    render(<SearchView />);

    act(() => navigation.goTo("/search?q=new"));
    await screen.findByRole("link", { name: "new.pdf" });
    await act(async () => old.reject(new Error("Old failure")));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "new.pdf" })).toBeInTheDocument();
  });

  it("aborts the search when leaving the page", async () => {
    navigation.reset("/search?q=cloud");
    const api = stubApi({ [ROUTE]: () => new Promise(() => undefined) });
    const { unmount } = render(<SearchView />);

    unmount();

    await waitFor(() => expect(api.calls(ROUTE)[0].init?.signal?.aborted).toBe(true));
  });

  // BUG, components/SearchView.tsx:51-62: the effect only runs when the URL changes, so pressing
  // Search again for the same words after a failure does nothing and the error stays.
  it("retries when the same query is submitted again after an error", async () => {
    navigation.reset("/search?q=cloud");
    let failing = true;
    const api = stubApi({
      [ROUTE]: () => (failing ? errorResponse("Try later.", 503) : hitsFor("found.pdf")),
    });
    render(<SearchView />);
    await screen.findByRole("alert");

    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await act(async () => undefined);

    expect(api.calls(ROUTE)).toHaveLength(2);
  });

  // BUG (robustness), components/SearchView.tsx:55: `.then` has no aborted check, unlike `.catch`.
  // If an aborted request still resolves, its outcome has the old key, which discards the newer
  // outcome and leaves "Searching…" forever. A real fetch rejects on abort, so this needs a request
  // that had already resolved, or a wrapper that ignores the signal.
  it("ignores an aborted request that resolves anyway", async () => {
    navigation.reset("/search?q=old");
    const old = Promise.withResolvers<Response>();
    const fetchMock = vi.fn<typeof fetch>((input) =>
      String(input).includes("q=old")
        ? old.promise
        : Promise.resolve(new FakeResponse(hitsFor("new.pdf")) as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SearchView />);

    act(() => navigation.goTo("/search?q=new"));
    await screen.findByRole("link", { name: "new.pdf" });
    await act(async () => old.resolve(new FakeResponse(hitsFor("old.pdf")) as unknown as Response));

    expect(screen.getByRole("link", { name: "new.pdf" })).toBeInTheDocument();
  });
});
