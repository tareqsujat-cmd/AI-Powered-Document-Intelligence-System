"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface PollingResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

const DEFAULT_ERROR = "Something went wrong while loading.";

/**
 * Fetch now, then repeatedly. Passing a new `load` (wrapped in useCallback) fetches at once.
 * An answer that arrives after a newer request started is dropped, so a slow response can never
 * overwrite a newer one. A hidden browser tab stops polling.
 */
export function usePolling<T>(
  load: (signal: AbortSignal) => Promise<T>,
  intervalMs: number | ((data: T | null) => number),
): PollingResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const dataRef = useRef<T | null>(null);
  const intervalRef = useRef(intervalMs);
  const runRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    intervalRef.current = intervalMs;
  }, [intervalMs]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    let stopped = false;

    const schedule = () => {
      clearTimeout(timer);
      if (stopped) return;
      const interval = intervalRef.current;
      const delay = typeof interval === "function" ? interval(dataRef.current) : interval;
      timer = setTimeout(run, delay);
    };

    function run() {
      clearTimeout(timer);
      controller?.abort();
      if (document.hidden) {
        schedule();
        return;
      }
      const current = new AbortController();
      controller = current;
      load(current.signal)
        .then((value) => {
          if (stopped || current.signal.aborted) return;
          dataRef.current = value;
          setData(value);
          setError(null);
          setLoading(false);
          schedule();
        })
        .catch((problem: unknown) => {
          if (stopped || current.signal.aborted) return;
          setError(problem instanceof Error ? problem.message : DEFAULT_ERROR);
          setLoading(false);
          // What does not exist will not start to exist, so there is no point asking again.
          if (!(problem instanceof ApiError && problem.status === 404)) schedule();
        });
    }

    const onVisible = () => {
      if (!document.hidden) run();
    };

    runRef.current = run;
    document.addEventListener("visibilitychange", onVisible);
    run();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const refresh = useCallback(() => runRef.current(), []);
  return { data, error, loading, refresh };
}
