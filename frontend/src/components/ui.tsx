import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { DocumentStatus, Severity } from "@/lib/api";
import { confidenceLevel, formatPercent } from "@/lib/format";

const BUTTON_STYLES = {
  primary: "bg-indigo-700 text-white hover:bg-indigo-800 disabled:bg-slate-300",
  secondary:
    "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:text-slate-400",
  danger: "border border-red-300 bg-white text-red-700 hover:bg-red-50 disabled:text-slate-400",
  ghost: "text-indigo-700 hover:bg-indigo-50 disabled:text-slate-400",
} as const;

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof BUTTON_STYLES }) {
  return (
    <button
      type="button"
      className={`inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-700 disabled:cursor-not-allowed ${BUTTON_STYLES[variant]} ${className}`}
      {...props}
    />
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white ${className}`}>
      {children}
    </section>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
      {children}
    </p>
  );
}

const STATUS_STYLES: Record<DocumentStatus, string> = {
  queued: "bg-slate-100 text-slate-700",
  processing: "bg-sky-100 text-sky-800",
  completed: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
};

const STATUS_LABELS: Record<DocumentStatus, string> = {
  queued: "Queued",
  processing: "Processing",
  completed: "Completed",
  failed: "Failed",
};

function Pill({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${className}`}
    >
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return <Pill className={STATUS_STYLES[status]}>{STATUS_LABELS[status]}</Pill>;
}

export function ReviewBadge() {
  return <Pill className="bg-amber-100 text-amber-900">Needs review</Pill>;
}

const SEVERITY_STYLES: Record<Severity, string> = {
  error: "bg-red-100 text-red-800",
  warning: "bg-amber-100 text-amber-900",
  info: "bg-slate-100 text-slate-700",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <Pill className={SEVERITY_STYLES[severity]}>{severity}</Pill>;
}

export function ProgressBar({ percent, label }: { percent: number; label: string }) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={clamped}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200"
    >
      <div
        className="h-full rounded-full bg-indigo-600 transition-[width] duration-300"
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

const CONFIDENCE_STYLES = {
  high: { bar: "bg-emerald-500", text: "text-emerald-800" },
  medium: { bar: "bg-amber-500", text: "text-amber-800" },
  low: { bar: "bg-red-500", text: "text-red-800" },
};

export function ConfidenceMeter({ confidence }: { confidence: number }) {
  const styles = CONFIDENCE_STYLES[confidenceLevel(confidence)];
  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={`Confidence ${formatPercent(confidence)}`}
    >
      <span aria-hidden="true" className="h-1 w-10 overflow-hidden rounded-full bg-slate-200">
        <span
          className={`block h-full rounded-full ${styles.bar}`}
          style={{ width: `${confidence * 100}%` }}
        />
      </span>
      <span className={`text-xs tabular-nums ${styles.text}`}>{formatPercent(confidence)}</span>
    </span>
  );
}

export function FormLabel({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium text-slate-600">
      {label}
      {children}
    </label>
  );
}

export const INPUT_CLASS =
  "rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-normal text-slate-900 placeholder:text-slate-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600";
