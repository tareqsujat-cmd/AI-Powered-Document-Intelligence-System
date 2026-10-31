import { act, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiStatus } from "@/components/ApiStatus";

function healthResponse(status = 200): Response {
  return new Response(
    JSON.stringify({ status: "ok", service: "backend", version: "0.1.0" }),
    { status, headers: { "Content-Type": "application/json" } },
  );
}

function stubFetch() {
  const fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ApiStatus", () => {
  it("shows the checking state while the request is pending", () => {
    stubFetch().mockReturnValue(new Promise<Response>(() => {}));

    render(<ApiStatus />);

    expect(screen.getByRole("status")).toHaveTextContent("Checking API...");
  });

  it("shows online when /health returns 2xx", async () => {
    stubFetch().mockResolvedValue(healthResponse());

    render(<ApiStatus />);

    expect(await screen.findByText("API online")).toBeInTheDocument();
  });

  it("shows unreachable when the request rejects", async () => {
    stubFetch().mockRejectedValue(new TypeError("Failed to fetch"));

    render(<ApiStatus />);

    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });

  it.each([404, 500, 503])(
    "shows unreachable when /health returns HTTP %i",
    async (status) => {
      stubFetch().mockResolvedValue(healthResponse(status));

      render(<ApiStatus />);

      expect(await screen.findByText("API unreachable")).toBeInTheDocument();
    },
  );

  it("requests /health on the configured API URL", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test");
    const fetchMock = stubFetch().mockResolvedValue(healthResponse());

    render(<ApiStatus />);
    await screen.findByText("API online");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "https://api.example.test/health",
    );
  });

  it("falls back to localhost:8000 when no API URL is configured", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", undefined);
    const fetchMock = stubFetch().mockResolvedValue(healthResponse());

    render(<ApiStatus />);
    await screen.findByText("API online");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://localhost:8000/health");
  });

  it("aborts the in-flight request on unmount", () => {
    const fetchMock = stubFetch().mockReturnValue(
      new Promise<Response>(() => {}),
    );

    const { unmount } = render(<ApiStatus />);
    const signal = fetchMock.mock.calls[0]?.[1]?.signal;
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
  });

  it.each<[string, (stale: PromiseWithResolvers<Response>) => void]>([
    ["error response", (stale) => stale.resolve(healthResponse(503))],
    ["rejection", (stale) => stale.reject(new TypeError("Failed to fetch"))],
  ])(
    "ignores a late %s from a request aborted by a Strict Mode remount",
    async (_outcome, settleStale) => {
      const stale = Promise.withResolvers<Response>();
      stubFetch()
        .mockReturnValueOnce(stale.promise)
        .mockResolvedValueOnce(healthResponse());

      render(
        <StrictMode>
          <ApiStatus />
        </StrictMode>,
      );
      await screen.findByText("API online");

      await act(async () => {
        settleStale(stale);
        await stale.promise.catch(() => undefined);
      });

      expect(screen.getByRole("status")).toHaveTextContent("API online");
    },
  );
});
