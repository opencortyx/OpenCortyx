"use client";
import { useEffect, useState } from "react";
import TagInput from "@/components/ui/TagInput";
import { graphTreeApi } from "@/lib/api/graphTree";

/**
 * An asset's tags, saved as they are edited. The graph files assets under their tags; an untagged
 * asset is filed under its file extension, which the placeholder names.
 */
export default function AssetTags({ assetId, onSaved }: { assetId: string; onSaved?: (tags: string[]) => void }) {
  const [tags, setTags] = useState<string[] | null>(null);
  const [fallback, setFallback] = useState("");
  const [state, setState] = useState<"" | "saving" | "saved" | "error">("");
  useEffect(() => {
    let cancelled = false;
    graphTreeApi.tags(assetId).then((r) => { if (!cancelled) { setTags(r.tags); setFallback(r.default); } })
      .catch(() => { if (!cancelled) setTags([]); });
    return () => { cancelled = true; };
  }, [assetId]);
  const save = async (next: string[]) => {
    setTags(next);
    setState("saving");
    try {
      const r = await graphTreeApi.setTags(assetId, next);
      setTags(r.tags);
      setState("saved");
      onSaved?.(r.tags);
    } catch { setState("error"); }
  };
  if (tags == null) return <div className="text-xs text-stone-400">Loading tags…</div>;
  return (
    <div>
      <TagInput value={tags} onChange={save} placeholder={fallback ? `Untagged: filed under “${fallback}”` : "Add a tag…"} />
      {state && (
        <div className={`text-[11px] mt-1 ${state === "error" ? "text-red-600" : "text-stone-400"}`}>
          {state === "saving" ? "Saving…" : state === "saved" ? "Saved" : "Could not save the tags"}
        </div>
      )}
    </div>
  );
}
