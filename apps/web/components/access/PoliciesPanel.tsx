"use client";

import { useCallback, useEffect, useState } from "react";
import { Eye, Lock, Plus, Trash2 } from "lucide-react";
import Button from "@/components/ui/Button";
import { accessApi, apiError, type AccessPreview, type PolicyList, type Sensitivity } from "@/lib/api/governance";

const SENS_HELP: Record<Sensitivity, string> = {
  public: "only public records",
  normal: "normal records (the default)",
  confidential: "also confidential records and personal-data columns",
  restricted: "everything, restricted included",
};

/**
 * Who reads which collection. The first policy turns access control on: from then on a person or an
 * API key reads the collections their groups are given (plus records in no collection), and nothing
 * else - in answers, search, browsing and the graph alike. Deleting the last policy turns it off.
 */
export default function PoliciesPanel({ users, onMessage }: {
  users: { id: string; email: string }[];
  onMessage: (text: string, ok: boolean) => void;
}) {
  const [data, setData] = useState<PolicyList | null>(null);
  const [form, setForm] = useState<{ group: string; collection_id: string; max_sensitivity: Sensitivity }>(
    { group: "", collection_id: "", max_sensitivity: "normal" });
  const [busy, setBusy] = useState("");
  const [who, setWho] = useState("");
  const [preview, setPreview] = useState<AccessPreview | null>(null);

  const load = useCallback(async () => {
    try { setData(await accessApi.policies()); }
    catch (e) { onMessage(apiError(e, "Could not load the access policies."), false); }
  }, [onMessage]);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    const group = form.group.trim();
    if (!group) { onMessage("Name the group the policy is for.", false); return; }
    if (!data?.enabled && !confirm(
      "This is the first policy: access control turns ON for the whole workspace. From now on everyone reads only " +
      "the collections a policy gives their groups (admins read every collection). Continue?")) return;
    setBusy("add");
    try {
      const r = await accessApi.addPolicy(group, form.collection_id || null, form.max_sensitivity);
      onMessage(r.access_control_turned_on ? "Access control is on." : "Policy saved.", true);
      setForm({ group: "", collection_id: "", max_sensitivity: "normal" });
      await load();
    } catch (e) { onMessage(apiError(e, "Could not save the policy."), false); }
    finally { setBusy(""); }
  };
  const remove = async (id: string) => {
    const last = (data?.items.length || 0) <= 1;
    if (!confirm(last ? "This is the last policy: access control turns OFF and everyone reads everything again. Remove it?"
                      : "Remove this policy?")) return;
    setBusy(`del:${id}`);
    try {
      const r = await accessApi.deletePolicy(id);
      onMessage(r.access_control_on ? "Policy removed." : "Access control is off.", true);
      await load();
    } catch (e) { onMessage(apiError(e, "Could not remove the policy."), false); }
    finally { setBusy(""); }
  };
  const check = async () => {
    if (!who) return;
    setBusy("preview");
    try {
      setPreview(await accessApi.preview(who.startsWith("user:") ? { user_id: who.slice(5) } : { group: who.slice(6) }));
    } catch (e) { onMessage(apiError(e, "Could not check access."), false); }
    finally { setBusy(""); }
  };

  if (!data) return <p className="text-sm text-stone-400 animate-pulse">Loading access policies…</p>;

  return (
    <div className="space-y-4">
      <div className={`rounded-lg border p-3 text-sm ${data.enabled ? "bg-emerald-50 border-emerald-200 text-emerald-800" : "bg-stone-50 border-stone-200 text-stone-600"}`}>
        {data.enabled
          ? "Access control is on: people and API keys read the collections their groups are given, and records in no collection."
          : "Access control is off: everyone in the workspace reads everything (normal records). Add a policy to turn it on."}
      </div>

      {data.collections.length === 0 && (
        <p className="text-xs text-amber-700">There are no collections yet. Put files in a collection when you add them (Add data), then give groups access to it here.</p>
      )}

      <div className="grid gap-2 sm:grid-cols-4 items-end">
        <label className="text-xs text-stone-500 space-y-1">
          <span>Group</span>
          <input list="known-groups" value={form.group} onChange={(e) => setForm({ ...form, group: e.target.value })}
            placeholder="e.g. finance" className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" />
          <datalist id="known-groups">{data.groups.map((g) => <option key={g} value={g} />)}</datalist>
        </label>
        <label className="text-xs text-stone-500 space-y-1">
          <span>Reads</span>
          <select value={form.collection_id} onChange={(e) => setForm({ ...form, collection_id: e.target.value })}
            className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white">
            <option value="">Every collection</option>
            {data.collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label className="text-xs text-stone-500 space-y-1">
          <span>Up to</span>
          <select value={form.max_sensitivity} onChange={(e) => setForm({ ...form, max_sensitivity: e.target.value as Sensitivity })}
            className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white" title={SENS_HELP[form.max_sensitivity]}>
            {data.sensitivities.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <Button variant="primary" size="sm" icon={Plus} busy={busy === "add"} disabled={!!busy || !form.group.trim()} onClick={add}>Add policy</Button>
      </div>
      <p className="text-[11px] text-stone-400">{SENS_HELP[form.max_sensitivity]}. A person&apos;s groups are their role and the groups on their account.</p>

      {data.items.length > 0 && (
        <ul className="divide-y divide-stone-100 border border-stone-200 rounded-lg bg-white">
          {data.items.map((p) => (
            <li key={p.id} className="flex items-center justify-between px-3 py-2 text-sm">
              <span><Lock size={13} className="inline mr-1.5 text-stone-400" /><b>{p.group}</b> reads <b>{p.collection || "?"}</b> up to <b>{p.max_sensitivity}</b></span>
              <button type="button" aria-label={`Remove the policy for ${p.group}`} onClick={() => remove(p.id)} disabled={!!busy}
                className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
            </li>
          ))}
        </ul>
      )}

      <div className="border-t border-stone-100 pt-3 space-y-2">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-stone-500 space-y-1 flex-1 min-w-[200px]">
            <span>What does this person or group read?</span>
            <select value={who} onChange={(e) => { setWho(e.target.value); setPreview(null); }}
              className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white">
              <option value="">Choose…</option>
              <optgroup label="People">{users.map((u) => <option key={u.id} value={`user:${u.id}`}>{u.email}</option>)}</optgroup>
              <optgroup label="Groups">{data.groups.map((g) => <option key={g} value={`group:${g}`}>{g}</option>)}</optgroup>
            </select>
          </label>
          <Button variant="secondary" size="sm" icon={Eye} busy={busy === "preview"} disabled={!who || !!busy} onClick={check}>Check</Button>
        </div>
        {preview && (
          <p className="text-sm text-stone-700" role="status">
            {preview.every_collection ? "Every collection" : (preview.collections?.length ? preview.collections.join(", ") : "No collection")}
            {" "}plus records in no collection, up to <b>{preview.max_sensitivity}</b> in answers.
          </p>
        )}
      </div>
    </div>
  );
}
