"use client";

/**
 * Deprecated route kept for existing links: forwards to the unified pipeline builder, keeping `?id=`.
 * `useSearchParams` needs the Suspense boundary, or the static build fails on this page.
 */

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";

function Redirect() {
  const router = useRouter();
  const params = useSearchParams();
  useEffect(() => {
    const id = params.get("id");
    router.replace((id ? `/dashboard/pipelines/${id}` : "/dashboard/pipelines/new?kind=structured") as any);
  }, [router, params]);
  return null;
}

export default function DeprecatedStructuredPipelinePage() {
  return (
    <Suspense fallback={null}>
      <Redirect />
      <div className="p-6 flex items-center gap-2 text-sm text-stone-500">
        <Loader2 size={16} className="animate-spin" /> opening the pipeline builder…
      </div>
    </Suspense>
  );
}
