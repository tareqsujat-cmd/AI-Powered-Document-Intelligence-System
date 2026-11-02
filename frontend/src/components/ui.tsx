import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { DocumentStatus, FileType, Severity } from "@/lib/api";
import { confidenceLevel, formatPercent } from "@/lib/format";
import { DocumentIcon, ErrorIcon, ImageIcon, SheetIcon } from "./icons";

const BUTTON_STYLES = {
  primary:
    "bg-indigo-600 text-white shadow-sm shadow-indigo-600/20 hover:bg-indigo-700 disabled:bg-slate-300 disabled:shadow-none",
  secondary:
    "bg-white text-slate-700 shadow-xs ring-1 ring-inset ring-slate-300 hover:bg-slate-50 hover:text-slate-900 disabled:bg-slate-50 disabled:text-slate-400 disabled:shadow-none",
  danger:
    "bg-white text-red-700 shadow-xs ring-1 ring-inset ring-red-300 hover:bg-red-50 disabled:text-slate-400",
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
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed ${BUTTON_STYLES[variant]} ${className}`}
      {...props}
    />
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section
      className={`rounded-xl bg-white shadow-sm shadow-slate-900/5 ring-1 ring-slate-900/5 ${className}`}
    >
      {children}
    </section>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p
      role="alert"
      className="flex items-start gap-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-inset ring-red-200"
    >
      <ErrorIcon className="mt-0.5 size-4 shrink-0 text-red-500" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

const STATUS_STYLES: Record<DocumentStatus, { pill: string; dot: string }> = {
  queued: { pill: "bg-slate-100 text-slate-700 ring-slate-500/15", dot: "bg-slate-400" },
  processing: { pill: "bg-sky-50 text-sky-800 ring-sky-600/20", dot: "bg-sky-500 animate-pulse" },
  completed: { pill: "bg-emerald-50 text-emerald-800 ring-emerald-600/20", dot: "bg-emerald-500" },
  failed: { pill: "bg-red-50 text-red-800 ring-red-600/20", dot: "bg-red-500" },
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
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${className}`}
    >
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const styles = STATUS_STYLES[status];
  return (
    <Pill className={styles.pill}>
      <span aria-hidden="true" className={`size-1.5 rounded-full ${styles.dot}`} />
      {STATUS_LABELS[status]}
    </Pill>
  );
}

export function ReviewBadge() {
  return (
    <Pill className="bg-amber-50 text-amber-900 ring-amber-600/25">
      <span aria-hidden="true" className="size-1.5 rounded-full bg-amber-500" />
      Needs review
    </Pill>
  );
}

const SEVERITY_STYLES: Record<Severity, string> = {
  error: "bg-red-50 text-red-800 ring-red-600/20",
  warning: "bg-amber-50 text-amber-900 ring-amber-600/25",
  info: "bg-slate-100 text-slate-700 ring-slate-500/15",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <Pill className={SEVERITY_STYLES[severity]}>{severity}</Pill>;
}

const FILE_ICONS: Record<FileType, { icon: typeof DocumentIcon; tint: string }> = {
  pdf: { icon: DocumentIcon, tint: "bg-rose-50 text-rose-600" },
  jpeg: { icon: ImageIcon, tint: "bg-violet-50 text-violet-600" },
  png: { icon: ImageIcon, tint: "bg-violet-50 text-violet-600" },
  tiff: { icon: ImageIcon, tint: "bg-violet-50 text-violet-600" },
  xlsx: { icon: SheetIcon, tint: "bg-emerald-50 text-emerald-600" },
  xls: { icon: SheetIcon, tint: "bg-emerald-50 text-emerald-600" },
  csv: { icon: SheetIcon, tint: "bg-teal-50 text-teal-600" },
};

/** Colour by family of file, so a long list can be scanned at a glance. */
export function FileTypeIcon({ type, size = "md" }: { type: FileType; size?: "md" | "lg" }) {
  const { icon: Glyph, tint } = FILE_ICONS[type];
  const box = size === "lg" ? "size-11 rounded-xl" : "size-9 rounded-lg";
  return (
    <span className={`flex shrink-0 items-center justify-center ${box} ${tint}`}>
      <Glyph className={size === "lg" ? "size-6" : "size-5"} />
    </span>
  );
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
        className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500 transition-[width] duration-500 ease-out"
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

const CONFIDENCE_STYLES = {
  high: { bar: "bg-emerald-500", text: "text-emerald-700" },
  medium: { bar: "bg-amber-500", text: "text-amber-700" },
  low: { bar: "bg-red-500", text: "text-red-700" },
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
      <span className={`text-xs font-medium tabular-nums ${styles.text}`}>
        {formatPercent(confidence)}
      </span>
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
  "rounded-lg border-0 bg-white px-2.5 py-1.5 text-sm font-normal text-slate-900 shadow-xs ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-indigo-600";

/** A section title inside a card. */
export function CardTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
      <h2 className="text-sm font-semibold text-slate-900">{children}</h2>
      {aside}
    </div>
  );
}

export function EmptyState({
  icon,
  children,
}: {
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      <span className="flex size-12 items-center justify-center rounded-full bg-slate-100 text-slate-400">
        {icon}
      </span>
      <div className="max-w-sm text-sm text-slate-500">{children}</div>
    </Card>
  );
}
