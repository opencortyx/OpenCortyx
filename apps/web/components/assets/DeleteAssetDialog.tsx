"use client";
import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, Trash2, X } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, AssetDeletePreview, AssetDeleteResult } from "@/lib/api/contextV2";

/**
 * Permanent asset deletion — the asset node plus everything derived from it (chunks, properties,
 * rows, entities, claims, summaries, comments), every edge that touches them, its progress runs and
 * its sync-manifest entry. There is no undo, so the dialog first asks the API what WOULD be removed
 * (dry run) and makes the user type DELETE before the button arms.
 */
export default function DeleteAssetDialog({
  assetId,
  assetName,
  onClose,
  onDeleted,
}: {
  assetId: string;
  assetName: string;
  onClose: () => void;
  onDeleted?: (res: AssetDeleteResult) => void;
}) {
  const [preview, setPreview] = useState<AssetDeletePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    contextV2Api
      .previewAssetDelete(assetId)
      .then((p) => alive && setPreview(p))
      .catch((e: any) => alive && setError(e?.response?.data?.detail || e?.message || "Failed to load"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [assetId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const armed = confirmText.trim().toUpperCase() === "DELETE" && !loading && !busy;

  const doDelete = async () => {
    if (!armed) return;
    setBusy(true);
    setError(null);
    try {
      const res = await contextV2Api.deleteAsset(assetId);
      onDeleted?.(res);
      onClose();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Delete failed");
      setBusy(false);
    }
  };

  const types = Object.entries(preview?.by_type || {});

  return (
    <div
      className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4"
      onClick={() => !busy && onClose()}
    >
      <div
        className="bg-white dark:bg-stone-900 rounded-2xl shadow-2xl w-full max-w-lg max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-stone-100 dark:border-stone-800 flex items-center justify-between">
          <h2 className="font-bold text-stone-900 dark:text-stone-100 flex items-center gap-2">
            <AlertTriangle size={18} className="text-red-500" /> Delete asset permanently
          </h2>
          <button
            onClick={onClose}
            disabled={busy}
            className="p-1.5 text-stone-400 hover:bg-stone-100 dark:hover:bg-stone-800 rounded-md disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-6 space-y-4">
          <p className="text-sm text-stone-600 dark:text-stone-400">
            <span className="font-semibold text-stone-900 dark:text-stone-100">{assetName}</span> and
            everything derived from it will be erased from the database — nodes, edges, extracted
            entities and claims, progress history. This cannot be undone.
          </p>

          <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50/60 dark:bg-red-950/30 p-4">
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-stone-500">
                <Loader2 size={15} className="animate-spin" /> Counting what will be removed…
              </div>
            ) : (
              <>
                <div className="flex items-center gap-6 text-sm">
                  <div>
                    <div className="text-[11px] uppercase tracking-wider text-stone-500 font-semibold">Nodes</div>
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">
                      {preview?.nodes?.toLocaleString() ?? 0}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] uppercase tracking-wider text-stone-500 font-semibold">Edges</div>
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">
                      {preview?.edges === null || preview?.edges === undefined
                        ? "all linked"
                        : preview.edges.toLocaleString()}
                    </div>
                  </div>
                </div>
                {types.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {types.map(([t, c]) => (
                      <span
                        key={t}
                        className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-white dark:bg-stone-900 border border-red-200 dark:border-red-900 text-stone-700 dark:text-stone-300"
                      >
                        {t} · {c.toLocaleString()}
                      </span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          <div>
            <label className="block text-[11px] uppercase tracking-wider text-stone-500 font-semibold mb-1.5">
              Type DELETE to confirm
            </label>
            <input
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doDelete()}
              placeholder="DELETE"
              className="w-full px-3 py-2 border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-800 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-red-500/30"
            />
          </div>

          {error && (
            <div className="text-xs text-red-600 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2">
              {error}
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t border-stone-100 dark:border-stone-800 flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" icon={Trash2} busy={busy} disabled={!armed} onClick={doDelete}>
            Delete permanently
          </Button>
        </div>
      </div>
    </div>
  );
}
