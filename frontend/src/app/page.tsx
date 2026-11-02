import { Suspense } from "react";
import { Dashboard } from "@/components/Dashboard";

export default function Home() {
  return (
    <Suspense>
      <Dashboard />
    </Suspense>
  );
}
