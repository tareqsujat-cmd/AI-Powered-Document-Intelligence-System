import type { Metadata } from "next";
import { Suspense } from "react";
import { DocumentView } from "@/components/DocumentView";

export const metadata: Metadata = { title: "Document · Document Intelligence" };

export default async function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Suspense>
      <DocumentView key={id} documentId={id} />
    </Suspense>
  );
}
