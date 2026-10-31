import { ApiStatus } from "@/components/ApiStatus";

export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col items-start gap-4 px-6 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">
        Document Intelligence
      </h1>
      <p className="text-slate-600">
        AI-powered invoice extraction, validation and search.
      </p>
      <ApiStatus />
    </main>
  );
}
