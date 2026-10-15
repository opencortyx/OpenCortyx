"use client";

import { useEffect, useState } from "react";
import { contextV2Api, AppUser, ApiKeyView } from "@/lib/api/contextV2";
import {
  Users, Plus, Trash2, KeyRound, ShieldCheck, AlertCircle, Loader2, Copy, X, Ban, CheckCircle2,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

const ROLES = ["admin", "editor", "viewer"];
const roleTone: Record<string, string> = {
  admin: "bg-emerald-50 text-emerald-700 border-emerald-200",
  editor: "bg-sky-50 text-sky-700 border-sky-200",
  viewer: "bg-stone-100 text-stone-600 border-stone-200",
};

export default function UsersPage() {
  const [users, setUsers] = useState<AppUser[]>([]);
  const [keys, setKeys] = useState<ApiKeyView[]>([]);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);
  const [msg, setMsg] = useState({ text: "", type: "" });

  // add-user form
  const [nu, setNu] = useState({ email: "", password: "", role: "viewer" });
  const [busy, setBusy] = useState(false);
  // new key reveal
  const [newKey, setNewKey] = useState<{ key: string; app: string } | null>(null);
  const [keyForm, setKeyForm] = useState({ app: "claude", role: "editor" });

  const load = async () => {
    setLoading(true); setDenied(false);
    try {
      const [u, k] = await Promise.all([contextV2Api.listUsers(), contextV2Api.listApiKeys()]);
      setUsers(u.items || []); setKeys(k.items || []);
    } catch (e: any) {
      if (e?.response?.status === 403) setDenied(true);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const addUser = async () => {
    if (!nu.email.trim() || nu.password.length < 6) { setMsg({ text: "Email + 6-char password required.", type: "error" }); return; }
    setBusy(true); setMsg({ text: "", type: "" });
    try {
      await contextV2Api.createUser(nu.email.trim(), nu.password, nu.role);
      setNu({ email: "", password: "", role: "viewer" }); await load();
      setMsg({ text: "User registered.", type: "ok" });
    } catch (e: any) { setMsg({ text: e?.response?.data?.detail || "Failed to add user.", type: "error" }); }
    finally { setBusy(false); }
  };

  const setRole = async (u: AppUser, role: string) => {
    try { await contextV2Api.updateUser(u.id, { role }); await load(); }
    catch (e: any) { setMsg({ text: e?.response?.data?.detail || "Failed.", type: "error" }); }
  };
  const toggleStatus = async (u: AppUser) => {
    try { await contextV2Api.updateUser(u.id, { status: u.status === "active" ? "disabled" : "active" }); await load(); }
    catch (e: any) { setMsg({ text: e?.response?.data?.detail || "Failed.", type: "error" }); }
  };
  const removeUser = async (u: AppUser) => {
    if (!confirm(`Remove ${u.email}?`)) return;
    try { await contextV2Api.deleteUser(u.id); await load(); }
    catch (e: any) { setMsg({ text: e?.response?.data?.detail || "Failed.", type: "error" }); }
  };

  const addKey = async () => {
    setBusy(true); setMsg({ text: "", type: "" });
    try {
      const res = await contextV2Api.createApiKey(keyForm.app.trim() || "claude", keyForm.role);
      setNewKey({ key: res.key, app: res.app }); await load();
    } catch (e: any) { setMsg({ text: e?.response?.data?.detail || "Failed to create key.", type: "error" }); }
    finally { setBusy(false); }
  };
  const revokeKey = async (id: string) => {
    if (!confirm("Revoke this API key? Agents using it will stop working.")) return;
    try { await contextV2Api.revokeApiKey(id); await load(); } catch { /* */ }
  };

  if (denied) {
    return (
      <div className="max-w-7xl mx-auto">
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-amber-800 flex items-center gap-3">
          <AlertCircle size={20} /> This page is for <b>admins</b> only.
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-8 pb-20">
      <PageHeader
        title="Users & Access"
        icon={Users}
        description="Register people for this workspace and set their role. Issue API keys for agents (Claude / MCP)."
      />

      {msg.text && (
        <div className={`p-3 rounded-lg text-sm font-medium flex items-center gap-2 ${msg.type === "error" ? "bg-red-50 text-red-700 border border-red-100" : "bg-emerald-50 text-emerald-700 border border-emerald-100"}`}>
          {msg.type === "error" ? <AlertCircle size={16} /> : <CheckCircle2 size={16} />} {msg.text}
        </div>
      )}

      {/* register user */}
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm p-5">
        <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide mb-3 flex items-center gap-2"><Plus size={15} className="text-emerald-500" /> Register a user</h2>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          <input type="email" name="new-user-email" autoComplete="off" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} placeholder="email" className="sm:col-span-2 border border-stone-200 rounded-lg px-3 py-2 text-sm" />
          <input type="password" name="new-user-password" autoComplete="new-password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} placeholder="temp password (6+)" className="border border-stone-200 rounded-lg px-3 py-2 text-sm" />
          <div className="flex gap-2">
            <select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })} className="flex-1 border border-stone-200 rounded-lg px-2 py-2 text-sm bg-white">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <Button variant="primary" busy={busy} onClick={addUser}>Add</Button>
          </div>
        </div>
        <p className="text-xs text-stone-400 mt-2">admin = full control · editor = read + write · viewer = read-only</p>
      </div>

      {/* users list */}
      <div>
        <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide mb-3">Members</h2>
        {loading ? (
          <div className="text-stone-500 animate-pulse p-4">Loading…</div>
        ) : (
          <div className="space-y-2">
            {users.map((u) => (
              <div key={u.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-semibold text-stone-800 truncate">{u.email}</div>
                  <div className="text-xs text-stone-400">{u.status === "active" ? "active" : "disabled"}{u.created_at ? ` · joined ${new Date(u.created_at).toLocaleDateString()}` : ""}</div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <select value={u.role} onChange={(e) => setRole(u, e.target.value)}
                    className={`text-xs font-bold uppercase px-2 py-1 rounded-md border bg-white ${roleTone[u.role] || ""}`}>
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <button onClick={() => toggleStatus(u)} title={u.status === "active" ? "Disable" : "Enable"}
                    className="p-1.5 text-stone-400 hover:text-amber-600 hover:bg-amber-50 rounded-md">{u.status === "active" ? <Ban size={15} /> : <CheckCircle2 size={15} />}</button>
                  <button onClick={() => removeUser(u)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* API keys */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide flex items-center gap-2"><KeyRound size={15} className="text-teal-500" /> Agent API keys</h2>
          <div className="flex items-center gap-2">
            <select value={keyForm.role} onChange={(e) => setKeyForm({ ...keyForm, role: e.target.value })} className="border border-stone-200 rounded-lg px-2 py-1.5 text-sm bg-white">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <Button variant="primary" size="sm" icon={Plus} busy={busy} onClick={addKey}>New key</Button>
          </div>
        </div>
        {newKey && (
          <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-4 mb-3">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-sm font-bold text-emerald-800 flex items-center gap-1.5"><ShieldCheck size={15} /> Copy this key now — it won&apos;t be shown again</span>
              <button onClick={() => setNewKey(null)} className="text-emerald-700 hover:bg-emerald-100 rounded p-1"><X size={15} /></button>
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-xs font-mono bg-white border border-emerald-200 rounded px-2 py-1.5 truncate">{newKey.key}</code>
              <button onClick={() => navigator.clipboard?.writeText(newKey.key)} className="p-1.5 text-emerald-700 hover:bg-emerald-100 rounded"><Copy size={15} /></button>
            </div>
            <p className="text-xs text-emerald-700 mt-2">Use it with <code>bin/opencortyx connect claude &lt;key&gt;</code>, as <code>OPENCORTYX_API_KEY</code>, or in the <code>X-API-Key</code> header.</p>
          </div>
        )}
        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-3 flex items-center justify-between gap-3">
              <div className="text-sm text-stone-700"><span className="font-semibold">{k.app}</span> <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border ml-1 ${roleTone[k.role] || ""}`}>{k.role}</span></div>
              <button onClick={() => revokeKey(k.id)} className="text-xs text-red-600 hover:underline">Revoke</button>
            </div>
          ))}
          {!loading && keys.length === 0 && <p className="text-sm text-stone-400">No agent keys yet.</p>}
        </div>
      </div>
    </div>
  );
}
