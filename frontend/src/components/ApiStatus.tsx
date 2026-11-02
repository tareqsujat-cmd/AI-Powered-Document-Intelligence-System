"use client";

import { useEffect, useState } from "react";

type ApiState = "checking" | "online" | "unreachable";

const STATE_VIEW: Record<ApiState, { label: string; dotClass: string }> = {
  checking: { label: "Checking API...", dotClass: "bg-amber-400" },
  online: { label: "API online", dotClass: "bg-emerald-500" },
  unreachable: { label: "API unreachable", dotClass: "bg-red-500" },
};

export function ApiStatus() {
  const [state, setState] = useState<ApiState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const baseUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

    async function checkHealth() {
      try {
        const response = await fetch(`${baseUrl}/health`, {
          signal,
          cache: "no-store",
        });
        if (!signal.aborted) {
          setState(response.ok ? "online" : "unreachable");
        }
      } catch {
        if (!signal.aborted) {
          setState("unreachable");
        }
      }
    }

    void checkHealth();

    return () => controller.abort();
  }, []);

  const { label, dotClass } = STATE_VIEW[state];

  return (
    <p
      role="status"
      className="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1 text-xs font-medium text-slate-700 shadow-xs ring-1 ring-inset ring-slate-200"
    >
      <span aria-hidden="true" className={`size-2 rounded-full ${dotClass}`} />
      {label}
    </p>
  );
}
