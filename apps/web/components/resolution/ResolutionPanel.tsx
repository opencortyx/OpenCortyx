"use client";

import { useEffect, useState } from "react";
import { GitMerge, RefreshCw, Save } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, type TypeResolution } from "@/lib/api/contextV2";

const NO_RESOLUTION: TypeResolution = {
  identify_by: [], compare: [], ignore: [], enabled: true, scorer: "exact", auto: 0.95, review: 0.8,
};

/**
 * When two nodes of this type are the same thing, for any type. A type an extractor creates can also
 * be set in that extractor's Resolution section; a file's kinds (Lead, Project, ...) in the Mapper
 * ("Same thing across files"). The choices are the attributes real nodes of the type carry.
 */
export default function ResolutionPanel({ name, flash, onSaved }: { name: string; flash: (t: string, ok?: boolean) => void; onSaved: () => void }) {
  const [spec, setSpec] = useState<TypeResolution | null>(null);
  const [attrs, setAttrs] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "save" | "run">("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    contextV2Api.getTypeResolution(name).then((r) => {
      setSpec({ ...NO_RESOLUTION, ...(r.resolution || {}) });
      setAttrs(r.attributes || []);
      setSaved(!!r.resolution?.identify_by?.length);
    }).catch(() => setSpec({ ...NO_RESOLUTION }));
  }, [name]);

  if (!spec) return <p className="text-xs text-stone-400 border-t border-stone-200 pt-3">Loading resolution…</p>;
  const set = (patch: Partial<TypeResolution>) => { setSpec({ ...spec, ...patch }); setSaved(false); };
  const toggle = (list: string[], v: string) => list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  // fields already chosen stay offered even when the sample no longer shows them
  const fields = Array.from(new Set([...spec.identify_by, ...attrs]));
  const others = fields.filter((f) => !spec.identify_by.includes(f));

  const save = async () => {
    setBusy("save");
    try {
      await contextV2Api.setTypeResolution(name, spec);
      setSaved(true);
      onSaved();
      flash(spec.identify_by.length ? `${name}: new assets are now matched by ${spec.identify_by.join(" + ")}`
                                    : `${name}: records are no longer matched`);
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      flash(Array.isArray(d) ? d[0]?.msg : d || "Could not save resolution", false);
    } finally { setBusy(""); }
  };
  const runAgain = async () => {
    setBusy("run");
    try {
      const r = await contextV2Api.resolveTypeAgain(name);
      flash(r.assets ? `Re-checking ${r.assets} asset(s) with ${name} records. Results appear in the Review Queue.`
                     : `No assets hold ${name} records yet`);
    } catch { flash("Could not queue the re-check", false); } finally { setBusy(""); }
  };

  return (
    <div className="border-t border-stone-200 pt-3 space-y-3">
      <div>
        <h5 className="text-xs font-bold text-stone-700 flex items-center gap-1.5"><GitMerge size={13} className="text-teal-600" /> Resolution</h5>
        <p className="text-[11.5px] text-stone-500 mt-0.5">
          When are two {name} records the same thing? Pick the fields that identify one. After each asset
          is ingested, its {name} records are matched with the ones you already have: matches are linked,
          missing values added, and different values flagged in the Review Queue. Nothing is overwritten.
        </p>
      </div>

      <div>
        <span className="text-[11px] font-bold text-stone-500">Identified by</span>
        <div className="flex flex-wrap gap-1.5 mt-1">
          {fields.map((f) => {
            const on = spec.identify_by.includes(f);
            return (
              <button key={f} type="button" aria-pressed={on}
                onClick={() => set({ identify_by: toggle(spec.identify_by, f), ignore: spec.ignore.filter((x) => x !== f) })}
                className={`px-2 py-0.5 rounded border text-[11px] font-mono ${on ? "bg-teal-600 border-teal-600 text-white" : "bg-white border-stone-200 text-stone-600 hover:border-teal-400"}`}>
                {f}
              </button>
            );
          })}
          {!fields.length && <span className="text-[11px] text-stone-400">No {name} records yet, so there are no fields to choose from. Ingest some first.</span>}
        </div>
        <p className="text-[11px] text-stone-400 mt-1">
          {spec.identify_by.length
            ? <>Two records are the same when their <b>{spec.identify_by.join(" + ")}</b> {spec.identify_by.length > 1 ? "match together" : "matches"}.</>
            : <>None picked: {name} records are never matched.</>}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-stone-600">
        <label className="flex items-center gap-1.5">Scorer
          <select value={spec.scorer} onChange={(e) => set({ scorer: e.target.value as TypeResolution["scorer"] })}
            className="px-2 py-1 rounded-md border border-stone-200 bg-white">
            <option value="exact">Exact (same values)</option>
            <option value="similar">Similar (close spellings)</option>
          </select>
        </label>
        {spec.scorer === "similar" ? (
          <>
            <label className="flex items-center gap-1.5">Apply automatically at
              <input type="number" min={0} max={1} step={0.05} value={spec.auto}
                onChange={(e) => set({ auto: Number(e.target.value) })} className="w-16 px-2 py-1 rounded-md border border-stone-200" />
            </label>
            <label className="flex items-center gap-1.5">Send for review at
              <input type="number" min={0} max={1} step={0.05} value={spec.review}
                onChange={(e) => set({ review: Number(e.target.value) })} className="w-16 px-2 py-1 rounded-md border border-stone-200" />
            </label>
          </>
        ) : <span className="text-stone-400">An exact match is applied automatically.</span>}
      </div>

      {others.length > 0 && (
        <div>
          <span className="text-[11px] font-bold text-stone-500">Never compare</span>
          <div className="flex flex-wrap gap-1.5 mt-1">
            {others.map((f) => {
              const on = spec.ignore.includes(f);
              return (
                <button key={f} type="button" aria-pressed={on} onClick={() => set({ ignore: toggle(spec.ignore, f) })}
                  className={`px-2 py-0.5 rounded border text-[11px] font-mono ${on ? "bg-stone-600 border-stone-600 text-white line-through" : "bg-white border-stone-200 text-stone-600 hover:border-stone-400"}`}>
                  {f}
                </button>
              );
            })}
          </div>
          <p className="text-[11px] text-stone-400 mt-1">Every other field is compared on a match. Pick fields whose differences do not matter, such as notes or timestamps.</p>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {saved && spec.identify_by.length > 0 && (
          <Button variant="secondary" size="sm" icon={RefreshCw} busy={busy === "run"} disabled={!!busy} onClick={runAgain}>
            Re-check existing data
          </Button>
        )}
        <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!!busy || saved} onClick={save}>
          {saved ? "Saved" : "Save resolution"}
        </Button>
      </div>
    </div>
  );
}
