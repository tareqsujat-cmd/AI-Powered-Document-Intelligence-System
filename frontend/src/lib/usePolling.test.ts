import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { usePolling } from "./usePolling";
import { advance, flush } from "@/test/fixtures";

type Load = (signal: AbortSignal) => Promise<string>;

/** A load whose every call waits for the test to answer it. */
function controlledLoad() {
  const calls: {
    signal: AbortSignal;
    resolve: (value: string) => void;
    reject: (reason: unknown) => void;
  }[] = [];
  const load = vi.fn<Load>((signal) => {
    const { promise, resolve, reject } = Promise.withResolvers<string>();
    calls.push({ signal, resolve, reject });
    return promise;
  });
  return { load, calls };
}

async function answer(call: { resolve: (value: string) => void }, value: string) {
  await act(async () => call.resolve(value));
}

async function fail(call: { reject: (reason: unknown) => void }, reason: unknown) {
  await act(async () => call.reject(reason));
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

function changeVisibility(hidden: boolean) {
  setHidden(hidden);
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  // Removes the own property, which puts the prototype's real getter back.
  delete (document as unknown as { hidden?: boolean }).hidden;
  vi.useRealTimers();
});

describe("usePolling on a 404", () => {
  it("stops asking for something that does not exist", async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockRejectedValue(new ApiError("Document not found.", 404));

    const { result } = renderHook(() => usePolling(load, 1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });

    expect(load).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBe("Document not found.");
    vi.useRealTimers();
  });

  it("keeps trying through a failure that may pass", async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockRejectedValue(new ApiError("The server had a problem.", 500));

    renderHook(() => usePolling(load, 1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(load.mock.calls.length).toBeGreaterThan(1);
    vi.useRealTimers();
  });
});

describe("usePolling", () => {
  it("loads at once, showing loading until the answer arrives", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    expect(load).toHaveBeenCalledTimes(1);
    expect(result.current).toMatchObject({ data: null, error: null, loading: true });

    await answer(calls[0], "first");

    expect(result.current).toMatchObject({ data: "first", error: null, loading: false });
  });

  it("polls again one interval after each answer, and not before it", async () => {
    const { load, calls } = controlledLoad();
    renderHook(() => usePolling(load, 1000));

    // The first answer is slow, and a slow request is not stacked on by another one.
    await advance(5000);
    expect(load).toHaveBeenCalledTimes(1);

    await answer(calls[0], "first");
    await advance(999);
    expect(load).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(load).toHaveBeenCalledTimes(2);

    await answer(calls[1], "second");
    await advance(1000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("shows each new answer", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    await answer(calls[0], "first");
    await advance(1000);
    await answer(calls[1], "second");

    expect(result.current.data).toBe("second");
  });

  it("gives a function interval the latest data", async () => {
    const { load, calls } = controlledLoad();
    const interval = vi.fn((data: string | null) => (data === "busy" ? 100 : 5000));
    renderHook(() => usePolling(load, interval));

    await answer(calls[0], "busy");
    expect(interval).toHaveBeenLastCalledWith("busy");
    await advance(100);
    expect(load).toHaveBeenCalledTimes(2);

    await answer(calls[1], "idle");
    expect(interval).toHaveBeenLastCalledWith("idle");
    await advance(4999);
    expect(load).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("uses a new fixed interval from the next schedule on, without fetching", async () => {
    const { load, calls } = controlledLoad();
    const { rerender } = renderHook(({ ms }) => usePolling(load, ms), {
      initialProps: { ms: 1000 },
    });

    rerender({ ms: 200 });
    expect(load).toHaveBeenCalledTimes(1);

    await answer(calls[0], "first");
    await advance(200);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("drops a slow old response that arrives after a newer one", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    act(() => result.current.refresh());
    expect(calls[0].signal.aborted).toBe(true);

    await answer(calls[1], "new");
    await answer(calls[0], "old");

    expect(result.current.data).toBe("new");
  });

  it("drops an old failure that arrives after a newer answer", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    act(() => result.current.refresh());
    await answer(calls[1], "new");
    await fail(calls[0], new Error("old failure"));

    expect(result.current).toMatchObject({ data: "new", error: null });
  });

  it("keeps the old data while showing the error from a failed poll", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    await answer(calls[0], "first");
    await advance(1000);
    await fail(calls[1], new Error("Server down"));

    expect(result.current).toMatchObject({ data: "first", error: "Server down", loading: false });
  });

  it("keeps polling after a failure and clears the error on the next success", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    await fail(calls[0], new Error("Server down"));
    expect(result.current).toMatchObject({ data: null, error: "Server down", loading: false });

    await advance(1000);
    expect(load).toHaveBeenCalledTimes(2);
    await answer(calls[1], "back");

    expect(result.current).toMatchObject({ data: "back", error: null });
  });

  it("has a default message for a failure that is not an Error", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    await fail(calls[0], "a string");

    expect(result.current.error).toBe("Something went wrong while loading.");
  });

  it("stops on unmount: the request is aborted and no timer is left", async () => {
    const { load, calls } = controlledLoad();
    const { unmount } = renderHook(() => usePolling(load, 1000));
    await answer(calls[0], "first");
    await advance(1000);
    expect(load).toHaveBeenCalledTimes(2);

    unmount();

    expect(calls[1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    await advance(10000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("does not reschedule when an answer arrives after unmount", async () => {
    const { load, calls } = controlledLoad();
    const { unmount } = renderHook(() => usePolling(load, 1000));

    unmount();
    await answer(calls[0], "late");

    expect(vi.getTimerCount()).toBe(0);
  });

  it("skips polls while the tab is hidden", async () => {
    const { load, calls } = controlledLoad();
    renderHook(() => usePolling(load, 1000));
    await answer(calls[0], "first");

    changeVisibility(true);
    await advance(10000);

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("does not fetch when the tab becomes hidden", async () => {
    const { load, calls } = controlledLoad();
    renderHook(() => usePolling(load, 1000));
    await answer(calls[0], "first");

    changeVisibility(true);

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("refreshes at once when the tab becomes visible, and polls on from there", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));
    await answer(calls[0], "first");
    changeVisibility(true);
    await advance(10000);

    changeVisibility(false);
    expect(load).toHaveBeenCalledTimes(2);
    await answer(calls[1], "fresh");
    expect(result.current.data).toBe("fresh");

    await advance(1000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("starts nothing when mounted in a hidden tab, then loads once it is visible", async () => {
    setHidden(true);
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));

    await advance(5000);
    expect(load).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(true);

    changeVisibility(false);
    await answer(calls[0], "first");
    expect(result.current).toMatchObject({ data: "first", loading: false });
  });

  it("refresh() fetches immediately and restarts the wait", async () => {
    const { load, calls } = controlledLoad();
    const { result } = renderHook(() => usePolling(load, 1000));
    await answer(calls[0], "first");
    await advance(900);

    act(() => result.current.refresh());
    expect(load).toHaveBeenCalledTimes(2);
    await answer(calls[1], "second");

    // The timer from before the refresh would have fired at 1000.
    await advance(999);
    expect(load).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("fetches at once with a new load function, and drops the old one's answer", async () => {
    const first = controlledLoad();
    const second = controlledLoad();
    const { result, rerender } = renderHook(({ load }) => usePolling(load, 1000), {
      initialProps: { load: first.load },
    });

    rerender({ load: second.load });

    expect(second.load).toHaveBeenCalledTimes(1);
    expect(first.calls[0].signal.aborted).toBe(true);

    await answer(second.calls[0], "from new");
    await answer(first.calls[0], "from old");
    expect(result.current.data).toBe("from new");

    await advance(1000);
    expect(first.load).toHaveBeenCalledTimes(1);
    expect(second.load).toHaveBeenCalledTimes(2);
  });

  it("does not fetch again on a re-render with the same load function", async () => {
    const { load, calls } = controlledLoad();
    const { rerender } = renderHook(() => usePolling(load, 1000));

    rerender();
    await answer(calls[0], "first");
    rerender();

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("shows the answer from a load that resolves straight away", async () => {
    const load = vi.fn<Load>(() => Promise.resolve("value"));
    const { result } = renderHook(() => usePolling(load, 1000));

    await flush();

    expect(result.current.data).toBe("value");
  });
});
