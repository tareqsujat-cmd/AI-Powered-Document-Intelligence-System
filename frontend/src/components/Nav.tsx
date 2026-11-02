"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ApiStatus } from "./ApiStatus";
import { DocumentIcon } from "./icons";

const LINKS = [
  { href: "/", label: "Documents" },
  { href: "/search", label: "Search" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-20 border-b border-slate-200/80 bg-white/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-6">
          <Link
            href="/"
            className="flex items-center gap-2.5 text-sm font-semibold tracking-tight text-slate-900"
          >
            <span className="flex size-8 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-sm shadow-indigo-600/30">
              <DocumentIcon className="size-4.5" />
            </span>
            Document Intelligence
          </Link>
          <nav aria-label="Main" className="flex gap-1">
            {LINKS.map((link) => {
              const active =
                link.href === "/"
                  ? pathname === "/" || pathname.startsWith("/documents")
                  : pathname.startsWith(link.href);
              return (
                <Link
                  key={link.href}
                  href={link.href}
                  aria-current={active ? "page" : undefined}
                  className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                    active
                      ? "bg-indigo-50 text-indigo-700"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  }`}
                >
                  {link.label}
                </Link>
              );
            })}
          </nav>
        </div>
        <ApiStatus />
      </div>
    </header>
  );
}
