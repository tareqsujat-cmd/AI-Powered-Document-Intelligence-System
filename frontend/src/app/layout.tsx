import type { Metadata } from "next";
import { Inter } from "next/font/google";
import type { ReactNode } from "react";
import { Nav } from "@/components/Nav";
import "./globals.css";

// Downloaded at build time and served from this app, so no browser contacts Google.
const inter = Inter({ subsets: ["latin"], display: "swap" });

export const metadata: Metadata = {
  title: "Document Intelligence",
  description: "AI-powered invoice extraction, validation and search.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en" className={inter.className}>
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <Nav />
        <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
