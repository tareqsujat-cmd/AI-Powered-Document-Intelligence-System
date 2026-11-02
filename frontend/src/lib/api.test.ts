import { describe, expect, it, vi } from "vitest";
import {
  API_URL,
  ApiError,
  correctResult,
  fileUrl,
  getDocument,
  getHistory,
  getMetrics,
  getPage,
  getResults,
  listDocuments,
  pageImageUrl,
  reprocessDocument,
  reprocessFailed,
  reviewIssue,
  search,
  uploadFile,
} from "./api";
import { FakeXhr, makeFile, makeOutcome, stubXhr } from "@/test/fixtures";

const API = `${API_URL}/api/v1`;

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function stubFetch(response: Response | Error | DOMException = json({})) {
  const fetchMock = vi.fn<typeof fetch>();
  if (response instanceof Response) fetchMock.mockResolvedValue(response);
  else fetchMock.mockRejectedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof stubFetch>) {
  return new URL(String(fetchMock.mock.calls[0]?.[0]));
}

function requestInit(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls[0]?.[1];
}

async function caught(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => {
      throw new Error("expected the request to fail");
    },
    (problem: unknown) => problem,
  );
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

describe("API base URL", () => {
  it("removes a trailing slash from the configured URL", async () => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test/");
    const fresh = await import("./api");
    expect(fresh.fileUrl("a")).toBe("https://api.example.test/api/v1/documents/a/file");
    vi.resetModules();
  });

  it("falls back to localhost:8000", async () => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const fresh = await import("./api");
    expect(fresh.API_URL).toBe("http://localhost:8000");
    vi.resetModules();
  });
});

describe("request URLs", () => {
  it("lists documents with the filters, limit and offset", async () => {
    const fetchMock = stubFetch();
    await listDocuments({ status: "failed", supplier: "A & B Ltd" }, 25, 50);

    const url = requestedUrl(fetchMock);
    expect(url.origin + url.pathname).toBe(`${API}/documents`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      status: "failed",
      supplier: "A & B Ltd",
      limit: "25",
      offset: "50",
    });
  });

  it("omits empty and undefined filters", async () => {
    const fetchMock = stubFetch();
    await listDocuments({ status: "", supplier: undefined, filename: "x" }, 25, 0);

    expect(Object.fromEntries(requestedUrl(fetchMock).searchParams)).toEqual({
      filename: "x",
      limit: "25",
      offset: "0",
    });
  });

  it.each<[string, () => Promise<unknown>, string]>([
    ["getMetrics", () => getMetrics(), "/metrics"],
    ["getDocument", () => getDocument("d1"), "/documents/d1"],
    ["getResults", () => getResults("d1"), "/documents/d1/results"],
    ["getPage", () => getPage("d1", 7), "/documents/d1/pages/7"],
    ["getHistory", () => getHistory("d1"), "/documents/d1/history"],
  ])("%s asks for the right path, uncached", async (_name, call, path) => {
    const fetchMock = stubFetch();
    await call();

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API}${path}`);
    expect(requestInit(fetchMock)?.cache).toBe("no-store");
    expect(requestInit(fetchMock)?.method).toBeUndefined();
  });

  it("passes the abort signal on", async () => {
    const fetchMock = stubFetch();
    const { signal } = new AbortController();
    await getDocument("d1", signal);
    expect(requestInit(fetchMock)?.signal).toBe(signal);
  });

  it("returns the parsed body", async () => {
    stubFetch(json({ id: "d1" }));
    await expect(getDocument("d1")).resolves.toEqual({ id: "d1" });
  });

  it("searches with the query, mode and limit, encoded", async () => {
    const fetchMock = stubFetch(json({ results: [] }));
    await search("cloud & hosting", "semantic");

    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/search");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: "cloud & hosting",
      mode: "semantic",
      limit: "50",
    });
  });

  it("builds the page image and file URLs", () => {
    expect(pageImageUrl("d1", 3)).toBe(`${API}/documents/d1/pages/3/image`);
    expect(fileUrl("d1")).toBe(`${API}/documents/d1/file`);
  });
});

describe("requests that change data", () => {
  it("PATCHes a correction as JSON with only the given fields", async () => {
    const fetchMock = stubFetch();
    await correctResult("d1", "r1", { supplier_name: "Acme", tax: null });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API}/documents/d1/results/r1`);
    const init = requestInit(fetchMock);
    expect(init?.method).toBe("PATCH");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({ supplier_name: "Acme", tax: null });
  });

  it.each([true, false])("PATCHes a review with is_resolved=%s", async (isResolved) => {
    const fetchMock = stubFetch();
    await reviewIssue("d1", "i1", isResolved);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API}/documents/d1/validation-errors/i1`);
    expect(requestInit(fetchMock)?.method).toBe("PATCH");
    expect(JSON.parse(String(requestInit(fetchMock)?.body))).toEqual({ is_resolved: isResolved });
  });

  it.each([true, false])("POSTs a reprocess with resume=%s and no body", async (resume) => {
    const fetchMock = stubFetch();
    await reprocessDocument("d1", resume);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API}/documents/d1/reprocess?resume=${resume}`);
    expect(requestInit(fetchMock)?.method).toBe("POST");
    expect(requestInit(fetchMock)?.body).toBeUndefined();
  });

  it("POSTs a retry of every failed document", async () => {
    const fetchMock = stubFetch(json({ queued: 2, failed_to_queue: 0 }));
    await expect(reprocessFailed()).resolves.toEqual({ queued: 2, failed_to_queue: 0 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API}/documents/reprocess-failed`);
    expect(requestInit(fetchMock)?.method).toBe("POST");
  });
});

describe("ApiError", () => {
  it("is an Error with a name, status and retry time", () => {
    const error = new ApiError("Nope", 429, 5);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({ name: "ApiError", message: "Nope", status: 429, retryAfterSeconds: 5 });
    expect(new ApiError("Nope", 500).retryAfterSeconds).toBeNull();
  });

  it("uses the API's detail string", async () => {
    stubFetch(json({ detail: "Document not found." }, 404));
    const error = await caught(getDocument("d1"));
    expect(error.message).toBe("Document not found.");
    expect(error.status).toBe(404);
  });

  it("uses the first msg when detail is a list", async () => {
    stubFetch(json({ detail: [{ msg: "first problem" }, { msg: "second problem" }] }, 422));
    expect((await caught(getDocument("d1"))).message).toBe("first problem");
  });

  it.each<[string, Response, string]>([
    ["a 429 with no detail", json({}, 429), "Too many requests. Try again shortly."],
    ["a 500 with no detail", json({}, 500), "The server had a problem. Try again shortly."],
    ["a 503 with an HTML body", new Response("<html>", { status: 503 }), "The server had a problem. Try again shortly."],
    ["a 400 with a non-JSON body", new Response("oops", { status: 400 }), "The request could not be completed."],
    ["an empty detail list", json({ detail: [] }, 422), "The request could not be completed."],
    ["a detail list with no msg", json({ detail: [{ loc: ["x"] }] }, 422), "The request could not be completed."],
    ["a detail of the wrong type", json({ detail: 42 }, 400), "The request could not be completed."],
    ["a null body", json(null, 400), "The request could not be completed."],
  ])("falls back sensibly for %s", async (_name, response, message) => {
    stubFetch(response);
    expect((await caught(getDocument("d1"))).message).toBe(message);
  });

  it("explains a network failure, with status 0", async () => {
    stubFetch(new TypeError("Failed to fetch"));
    const error = await caught(getDocument("d1"));
    expect(error.status).toBe(0);
    expect(error.message).toBe("The API could not be reached. Is the backend running?");
  });

  it("lets an AbortError through unchanged", async () => {
    const abort = new DOMException("Aborted", "AbortError");
    stubFetch(abort);
    await expect(getDocument("d1")).rejects.toBe(abort);
  });

  it.each<[string, Record<string, string>, number | null]>([
    ["a number of seconds", { "Retry-After": "30" }, 30],
    ["zero seconds", { "Retry-After": "0" }, 0],
    ["an HTTP date", { "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" }, null],
    ["no header", {}, null],
  ])("reads Retry-After from %s", async (_name, headers, seconds) => {
    stubFetch(json({}, 429, headers));
    expect((await caught(getDocument("d1"))).retryAfterSeconds).toBe(seconds);
  });

  // BUG (minor), lib/api.ts:207: a 2xx with a body that is not JSON, as from a proxy's HTML page,
  // rejects with a raw SyntaxError, so a person sees "Unexpected token '<'...".
  it("gives a readable error for a successful response that is not JSON", async () => {
    stubFetch(new Response("<html>", { status: 200 }));
    await expect(getDocument("d1")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("uploadFile", () => {
  function start() {
    stubXhr();
    const onProgress = vi.fn();
    const file = makeFile("scan.pdf", 10);
    const promise = uploadFile(file, onProgress);
    const xhr = FakeXhr.instances[0];
    return { promise, xhr, onProgress, file };
  }

  it("POSTs the file in the form under the field files", () => {
    const { xhr, promise } = start();

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe(`${API}/documents`);
    const sent = xhr.sent?.getAll("files") as File[];
    expect(sent).toHaveLength(1);
    expect(sent[0].name).toBe("scan.pdf");
    expect(sent[0].size).toBe(10);

    xhr.respond(200, { results: [makeOutcome()] });
    return promise;
  });

  it("reports progress as whole percentages", () => {
    const { xhr, onProgress, promise } = start();

    xhr.progress(0, 3);
    xhr.progress(1, 3);
    xhr.progress(3, 3);

    expect(onProgress.mock.calls).toEqual([[0], [33], [100]]);
    xhr.respond(200, { results: [makeOutcome()] });
    return promise;
  });

  it("ignores progress with no known length", () => {
    const { xhr, onProgress, promise } = start();

    xhr.progress(5, 0, false);

    expect(onProgress).not.toHaveBeenCalled();
    xhr.respond(200, { results: [makeOutcome()] });
    return promise;
  });

  it("resolves with the first outcome on success", async () => {
    const { xhr, promise } = start();
    const first = makeOutcome({ filename: "first.pdf" });

    xhr.respond(201, { results: [first, makeOutcome({ filename: "second.pdf" })] });

    await expect(promise).resolves.toEqual(first);
  });

  it("rejects with the API's detail and Retry-After on an error status", async () => {
    const { xhr, promise } = start();

    xhr.respond(429, { detail: "Slow down." }, { "Retry-After": "12" });

    const error = await caught(promise);
    expect(error).toMatchObject({ message: "Slow down.", status: 429, retryAfterSeconds: 12 });
  });

  it("rejects with a fallback when an error has no usable body", async () => {
    const { xhr, promise } = start();

    xhr.respond(502, "<html>Bad gateway</html>");

    const error = await caught(promise);
    expect(error.message).toBe("The server had a problem. Try again shortly.");
    expect(error.status).toBe(502);
    expect(error.retryAfterSeconds).toBeNull();
  });

  it("rejects when a success has no outcome in it", async () => {
    const { xhr, promise } = start();

    xhr.respond(200, { results: [] });

    expect((await caught(promise)).message).toBe("The request could not be completed.");
  });

  it("rejects on a network error, with status 0", async () => {
    const { xhr, promise } = start();

    xhr.failNetwork();

    const error = await caught(promise);
    expect(error.status).toBe(0);
    expect(error.message).toBe("The API could not be reached. Is the backend running?");
  });
});
