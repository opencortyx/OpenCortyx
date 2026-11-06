"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Eraser, RefreshCw, RotateCcw, Search, ShieldOff } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import { apiError, forgetApi, type DeletionJob, type SubjectPreview, type Tombstone } from "@/lib/api/governance";

type Hit = { id: string; label: string | null; type: string };

/**
 * Forget a person, a company - any record - from every store, with proof. Find it, see what would go
 * (its records, a merged record of them, what the platform remembers about it, and the documents that
 * also mention it, to forget whole if you choose), confirm by typing its name, and read the result:
 * one line per store, and what a search found afterwards. Deleting a file is on the file itself (Assets).
 */
export default function ForgetPage() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [picked, setPicked] = useState<Hit | null>(null);
  const [preview, setPreview] = useState<SubjectPreview | null>(null);
  const [docs, setDocs] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState("");
  const [jobs, setJobs] = useState<DeletionJob[]>([]);
  const [tombs, setTombs] = useState<Tombstone[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [denied, setDenied] = useState(false);

  const loadJobs = useCallback(async () => {
    try {
      const [j, t] = await Promise.all([forgetApi.jobs(50), forgetApi.tombstones()]);
      setJobs(j); setTombs(t);
    } catch (e: any) {
      if (e?.response?.status === 403) setDenied(true);
    }
  }, []);
  useEffect(() => { loadJobs(); }, [loadJobs]);

  useEffect(() => {
    if (q.trim().length < 2) { setHits([]); return; }
    const t = setTimeout(() => {
      contextV2Api.searchNodes(q.trim(), undefined, 15).then((r: any) => setHits(r.results || [])).catch(() => setHits([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  const pick = async (h: Hit) => {
    setPicked(h); setPreview(null); setDocs(new Set()); setConfirm(""); setMsg(null); setBusy("preview");
    try { setPreview(await forgetApi.previewSubject(h.id)); }
    catch (e) { setMsg({ text: apiError(e, "Could not read what would be forgotten."), ok: false }); }
    finally { setBusy(""); }
  };

  const run = async () => {
    if (!picked || !preview) return;
    setBusy("forget"); setMsg(null);
    try {
      const job = await forgetApi.forgetSubject(picked.id, confirm, Array.from(docs));
      setMsg(job.status === "done"
        ? { text: `${picked.label} is forgotten. Every store was checked afterwards and nothing was found.`, ok: true }
        : { text: `Not finished: ${job.error || "a step failed"}. Retry it below.`, ok: false });
      setPicked(null); setPreview(null); setQ(""); setHits([]); setConfirm("");
      await loadJobs();
    } catch (e) { setMsg({ text: apiError(e, "Could not forget it."), ok: false }); }
    finally { setBusy(""); }
  };

  const retry = async (j: DeletionJob) => {
    setBusy(`retry:${j.id}`);
    try {
      const r = await forgetApi.retry(j.id);
      setMsg(r.status === "done" ? { text: "Finished, and proven.", ok: true } : { text: r.error || "Still not finished.", ok: false });
      await loadJobs();
    } catch (e) { setMsg({ text: apiError(e, "Retry failed."), ok: false }); }
    finally { setBusy(""); }
  };

  const lift = async (t: Tombstone) => {
    if (!confirm_(`Allow this ${t.kind} to be read again? It is not restored - it is only no longer refused.`)) return;
    setBusy(`lift:${t.id}`);
    try { await forgetApi.lift(t.id); await loadJobs(); }
    catch (e) { setMsg({ text: apiError(e, "Could not lift it."), ok: false }); }
    finally { setBusy(""); }
  };

  if (denied) {
    return <div className="max-w-5xl mx-auto p-6"><div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-amber-800 flex items-center gap-3"><AlertCircle size={20} /> Forgetting is for <b>admins</b> only.</div></div>;
  }

  const matches = !!preview && confirm.trim().toLowerCase() === (picked?.label || "").trim().toLowerCase();

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6 pb-20">
      <PageHeader title="Data deletion" icon={Eraser}
        description="Forget a person, a company or any record from every store - graph, search, tables, evidence, chats, audit, caches - with proof. To delete a file, use Delete on the file in Assets." />

      {msg && (
        <div role="status" className={`p-3 rounded-lg text-sm flex items-center gap-2 ${msg.ok ? "bg-emerald-50 text-emerald-700 border border-emerald-100" : "bg-red-50 text-red-700 border border-red-100"}`}>
          {msg.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />} {msg.text}
        </div>
      )}

      <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-4" aria-label="Forget a subject">
        <label className="block">
          <span className="text-sm font-semibold text-stone-700">Who or what should be forgotten?</span>
          <div className="mt-1.5 flex items-center gap-2 border border-stone-200 rounded-lg px-3 py-2">
            <Search size={15} className="text-stone-400" />
            <input value={q} onChange={(e) => { setQ(e.target.value); setPicked(null); setPreview(null); }}
              placeholder="A name, as it appears in your data" className="flex-1 text-sm outline-none" aria-label="Search records" />
          </div>
        </label>
        {!picked && hits.length > 0 && (
          <ul className="border border-stone-100 rounded-lg divide-y divide-stone-100" aria-label="Matching records">
            {hits.map((h) => (
              <li key={h.id}>
                <button type="button" onClick={() => pick(h)} className="w-full text-left px-3 py-2 text-sm hover:bg-stone-50 flex justify-between">
                  <span className="text-stone-800">{h.label || "(no name)"}</span><span className="text-xs text-stone-400">{h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {!picked && q.trim().length >= 2 && hits.length === 0 && <p className="text-xs text-stone-400">No record with that name.</p>}

        {busy === "preview" && <p className="text-sm text-stone-500 animate-pulse">Reading what would be forgotten…</p>}

        {preview && picked && (
          <div className="space-y-4">
            <div className="rounded-lg bg-stone-50 border border-stone-100 p-4 text-sm space-y-2">
              <div><b>{preview.records.length}</b> record(s): {preview.records.map((r) => `${r.label} (${r.merged ? "merged record" : r.type})`).join(", ")}</div>
              <div><b>{preview.linked_records_only_about_it}</b> linked record(s) about nothing else</div>
              <div>Names searched for in chats, audit, labels and caches: {preview.names.map((n) => <code key={n} className="mx-0.5 px-1 bg-white border rounded">{n}</code>)}</div>
              <div><b>{preview.chat_messages}</b> chat message(s) name it (they are redacted) · <b>{preview.labels}</b> resolution label(s)</div>
            </div>

            <div>
              <h3 className="text-sm font-semibold text-stone-700">Documents that also mention it</h3>
              {preview.documents_mentioning.length === 0 ? <p className="text-xs text-stone-400 mt-1">None found.</p> : (
                <>
                  <p className="text-xs text-stone-500 mt-1">A document is kept unless you tick it. A kept document still mentions the name; ticked ones are forgotten whole.</p>
                  <ul className="mt-2 space-y-1">
                    {preview.documents_mentioning.map((d) => (
                      <li key={d.asset_id}>
                        <label className="flex items-center gap-2 text-sm">
                          <input type="checkbox" checked={docs.has(d.asset_id)}
                            onChange={(e) => { const n = new Set(docs); e.target.checked ? n.add(d.asset_id) : n.delete(d.asset_id); setDocs(n); }} />
                          <span>{d.label || d.asset_id}</span><span className="text-xs text-stone-400">mentions {d.mentions.join(", ")}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>

            <div className="border-t border-stone-100 pt-4 space-y-2">
              <label className="block text-sm text-stone-700">
                This cannot be undone. Type <b>{picked.label}</b> to confirm.
                <input value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Type the name to confirm"
                  className="mt-1.5 w-full border border-stone-200 rounded-lg px-3 py-2 text-sm" />
              </label>
              <div className="flex justify-end gap-2">
                <Button variant="secondary" size="sm" onClick={() => { setPicked(null); setPreview(null); setConfirm(""); }}>Cancel</Button>
                <Button variant="danger" size="sm" icon={Eraser} busy={busy === "forget"} disabled={!matches || !!busy} onClick={run}>
                  Forget everywhere{docs.size ? ` (+${docs.size} document${docs.size > 1 ? "s" : ""})` : ""}
                </Button>
              </div>
            </div>
          </div>
        )}
      </section>

      <section aria-label="Deletion history">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide">Deletion history</h2>
          <Button variant="ghost" size="sm" icon={RefreshCw} onClick={loadJobs}>Refresh</Button>
        </div>
        {jobs.length === 0 ? <p className="text-sm text-stone-400">Nothing has been deleted yet.</p> : (
          <div className="space-y-2">
            {jobs.map((j) => <JobRow key={j.id} job={j} busy={busy === `retry:${j.id}`} onRetry={() => retry(j)} />)}
          </div>
        )}
      </section>

      <section aria-label="Tombstones">
        <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide mb-1">Kept from coming back</h2>
        <p className="text-xs text-stone-500 mb-3">What was forgotten is refused if it is read again (a synced folder, an upload, a backup restore). Only a hash is kept.</p>
        {tombs.length === 0 ? <p className="text-sm text-stone-400">None.</p> : (
          <ul className="space-y-1">
            {tombs.map((t) => (
              <li key={t.id} className="flex items-center justify-between bg-white border border-stone-200 rounded-lg px-3 py-2 text-sm">
                <span><span className="font-semibold capitalize">{t.kind}</span> <code className="text-xs text-stone-400">{t.hash}…</code>
                  {t.created_at && <span className="text-xs text-stone-400"> · {new Date(t.created_at).toLocaleString()}</span>}</span>
                <Button variant="ghost" size="sm" icon={ShieldOff} busy={busy === `lift:${t.id}`} onClick={() => lift(t)}>Allow again</Button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function confirm_(text: string) { return typeof window === "undefined" ? false : window.confirm(text); }

function JobRow({ job, busy, onRetry }: { job: DeletionJob; busy: boolean; onRetry: () => void }) {
  const [open, setOpen] = useState(false);
  const remaining = Object.entries(job.proof?.remaining || {}).filter(([, v]) => Number(v) > 0);
  const kept = job.proof?.kept_documents_mentioning || [];
  return (
    <div className="bg-white border border-stone-200 rounded-xl p-3">
      <div className="flex items-center justify-between gap-3">
        <button type="button" onClick={() => setOpen(!open)} className="text-left text-sm min-w-0" aria-expanded={open}>
          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border mr-2 ${job.status === "done" ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}>{job.status}</span>
          <span className="font-semibold text-stone-800">{job.kind === "asset" ? "File" : "Subject"}</span>
          <span className="text-stone-500"> · {job.target?.label || job.target?.asset_id || job.target?.node_id}</span>
          {job.created_at && <span className="text-xs text-stone-400"> · {new Date(job.created_at).toLocaleString()}</span>}
        </button>
        {job.status !== "done" && <Button variant="secondary" size="sm" icon={RotateCcw} busy={busy} onClick={onRetry}>Retry</Button>}
      </div>
      {open && (
        <div className="mt-3 text-xs text-stone-600 space-y-2">
          <table className="w-full">
            <tbody>
              {Object.entries(job.steps || {}).map(([k, v]) => (
                <tr key={k} className="border-b border-stone-50">
                  <td className="py-1 pr-3 font-mono">{k}</td>
                  <td className={`py-1 pr-3 ${v.status === "done" ? "text-emerald-700" : "text-red-600"}`}>{v.status}</td>
                  <td className="py-1 pr-3 tabular-nums">{v.count ?? ""}</td>
                  <td className="py-1 text-stone-400">{v.error || v.note || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div>Proof: {remaining.length === 0 ? <span className="text-emerald-700">nothing found in any store</span>
            : <span className="text-red-600">still found in {remaining.map(([k, v]) => `${k} (${v})`).join(", ")}</span>}</div>
          {kept.length > 0 && <div>Kept on purpose, still mentioning it: {kept.map((d) => d.label || d.asset_id).join(", ")}</div>}
          {job.proof?.note && <div className="text-amber-700">{job.proof.note}</div>}
        </div>
      )}
    </div>
  );
}
