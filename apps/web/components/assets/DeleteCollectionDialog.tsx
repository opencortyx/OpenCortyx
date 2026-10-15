"use client";
import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, Trash2, X } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, CollectionDeletePreview } from "@/lib/api/contextV2";

/**
 * Permanent COLLECTION deletion — every node scoped to the collection (edges cascade), then the
 * collection row itself.
 *
 * This is the widest destructive action in the product. An asset delete removes one file's
 * subtree; this removes an entire corpus, which is why it asks the API what WOULD go first and
 * makes the user type the collection's own NAME rather than the word DELETE. Typing a generic
 * word proves you clicked; typing the name proves you know which corpus you are on.
 */
export default function DeleteCollectionDialog({
  collectionId,
  collectionName,
  onClose,
  onDeleted,
}: {
  collectionId: string;
  collectionName: string;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [preview, setPreview] = useState<CollectionDeletePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    contextV2Api
      .previewCollectionDelete(collectionId)
      .then((p) => alive && setPreview(p))
      .catch((e: any) => alive && setError(e?.response?.data?.detail || e?.message || "Failed to load"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [collectionId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const armed = confirmText.trim() === collectionName && !loading && !busy;

  const doDelete = async () => {
    if (!armed) return;
    setBusy(true);
    setError(null);
    try {
      await contextV2Api.deleteCollection(collectionId);
      onDeleted?.();
      onClose();
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "Delete failed");
      setBusy(false);
    }
  };

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
            <AlertTriangle size={18} className="text-red-500" /> Delete collection permanently
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
            Every node in{" "}
            <span className="font-semibold text-stone-900 dark:text-stone-100">{collectionName}</span>{" "}
            will be erased — turns, entities, facts, claims, attributes — along with every edge that
            touches them. This cannot be undone.
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
                    <div className="text-[11px] uppercase tracking-wider text-stone-500 font-semibold">
                      Nodes
                    </div>
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">
                      {preview?.nodes?.toLocaleString() ?? 0}
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] uppercase tracking-wider text-stone-500 font-semibold">
                      Edges
                    </div>
                    <div className="text-xl font-bold text-red-700 dark:text-red-400">
                      {preview?.edges?.toLocaleString() ?? 0}
                    </div>
                  </div>
                </div>
                {(preview?.by_type || []).length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {(preview?.by_type || []).slice(0, 14).map((row) => (
                      <span
                        key={row.type}
                        className="px-2 py-0.5 rounded-md bg-white dark:bg-stone-900 border border-red-200 dark:border-red-900 text-[11px] text-stone-600 dark:text-stone-400"
                      >
                        {row.type} · {row.count.toLocaleString()}
                      </span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {/* A large delete runs for minutes: every cascaded edge fires the degree-delta and
              write-accounting triggers row by row. Saying so up front stops a working delete
              from being mistaken for a hung one and killed halfway. */}
          {(preview?.edges ?? 0) > 10000 && (
            <p className="text-xs text-stone-500 dark:text-stone-400">
              This is a large collection — expect several minutes. Degree and accounting are
              maintained per edge, and the whole delete is one transaction, so interrupting it
              rolls everything back rather than leaving the graph half-deleted.
            </p>
          )}

          <div>
            <label className="text-xs font-semibold text-stone-600 dark:text-stone-400">
              Type <span className="font-mono text-stone-900 dark:text-stone-100">{collectionName}</span> to confirm
            </label>
            <input
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              disabled={busy}
              placeholder={collectionName}
              className="mt-1.5 w-full px-3 py-2 rounded-lg border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-950 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-red-400 disabled:opacity-50"
            />
          </div>

          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/40 rounded-lg px-3 py-2">
              {error}
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t border-stone-100 dark:border-stone-800 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <button
            onClick={doDelete}
            disabled={!armed}
            className="px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-semibold hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
            {busy ? "Deleting…" : "Delete collection"}
          </button>
        </div>
      </div>
    </div>
  );
}
