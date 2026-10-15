"use client";

/**
 * Structured sources: connect a live SQL database and read its real schema.
 *
 * This matters more than it looks. Proposing rules from a CSV means guessing which column is a key
 * from its NAME — and that guess is exactly where the `customer_id`-in-`customers` trap lives: a
 * primary key whose name ends in `_id`, read as a foreign key, dropped from the node's properties,
 * and left behind as a factless duplicate. A database does not have to be guessed at. It states
 * which columns are primary keys and which are foreign keys, and `proposeFromTables` uses that
 * answer instead of inventing one.
 *
 * What it deliberately does NOT do is fabricate rows. Introspection returns a schema, not data, so
 * the graph preview stays empty and says so rather than drawing a picture of records that do not
 * exist.
 */

import { useEffect, useState } from "react";
import { Database, Loader2, AlertTriangle, Table2, KeyRound, Link2, RefreshCw, Wand2 } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import type { SqlTable } from "./model";
import { Hint, Label } from "./panels";

const input =
  "w-full px-2.5 py-2 rounded-lg border border-stone-200 bg-white text-sm text-stone-800 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40";
const mono = "font-mono text-[12.5px]";

/** Where detected tables came from — so a sample run can read the same source, not a copy of it. */
export type SqlOrigin = { connector_id?: string; connection_string?: string; schema_name?: string };

export function SqlConnectBar({ onTables }: {
  onTables: (t: SqlTable[], label: string, origin: SqlOrigin) => void;
}) {
  const [connectors, setConnectors] = useState<{ id: string; name: string; kind?: string }[]>([]);
  const [connId, setConnId] = useState("");
  const [dsn, setDsn] = useState("");
  const [schema, setSchema] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    contextV2Api.connectors()
      .then((r: any) => setConnectors((r?.connectors || []).map((c: any) => ({ id: c.id, name: c.name, kind: c.kind }))))
      .catch(() => setConnectors([]));
  }, []);

  const detect = async () => {
    if (!connId && !dsn.trim()) { setErr("Pick a saved connection, or paste a connection string."); return; }
    setBusy(true); setErr("");
    try {
      const res: any = await contextV2Api.introspectSql({
        ...(connId ? { connector_id: connId } : { connection_string: dsn.trim() }),
        ...(schema.trim() ? { schema_name: schema.trim() } : {}),
      });
      const tables: SqlTable[] = res?.tables || [];
      if (!tables.length) {
        setErr("Connected, but this source exposes no tables in that schema.");
        return;
      }
      onTables(tables, connId ? (connectors.find((c) => c.id === connId)?.name || "connection") : "connection string",
               { ...(connId ? { connector_id: connId } : { connection_string: dsn.trim() }),
                 ...(schema.trim() ? { schema_name: schema.trim() } : {}) });
    } catch (e: any) {
      // a failed introspect must SAY why — a silent empty table list reads as "no tables"
      setErr(e?.response?.data?.detail || e?.message || "Could not read that source");
    } finally { setBusy(false); }
  };

  return (
    <div className="px-3.5 py-3 bg-stone-50/70 border-t border-stone-200">
      <p className="text-[11.5px] text-stone-500 mb-2.5">
        Read a live database and let its own keys drive the rules — no guessing which column is an id.
      </p>
      <div className="grid gap-2">
        {!!connectors.length && (
          <select className={`${input} ${mono}`} value={connId}
            onChange={(e) => { setConnId(e.target.value); setErr(""); }}>
            <option value="">saved connection…</option>
            {connectors.map((c) => <option key={c.id} value={c.id}>{c.name}{c.kind ? ` · ${c.kind}` : ""}</option>)}
          </select>
        )}
        {!connId && (
          <input className={`${input} ${mono}`} value={dsn} onChange={(e) => setDsn(e.target.value)}
            placeholder="postgresql://user:pass@host:5432/db" />
        )}
        <div className="flex gap-2">
          <input className={`${input} ${mono}`} value={schema} onChange={(e) => setSchema(e.target.value)}
            placeholder="schema (optional)" />
          <Button variant="secondary" size="sm" icon={Database} busy={busy} onClick={detect}>Detect tables</Button>
        </div>
      </div>
      {err && (
        <div className="mt-2 flex items-start gap-1.5 text-[11.5px] text-red-700">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />{err}
        </div>
      )}
    </div>
  );
}

export function SqlSchemaSource({ tables, source, onPropose, onReconnect, proposed }: {
  tables: SqlTable[]; source: string;
  onPropose: () => void; onReconnect: () => void; proposed: boolean;
}) {
  const [q, setQ] = useState("");
  const shown = tables.filter((t) => !q.trim() || t.name.toLowerCase().includes(q.trim().toLowerCase()));
  const pkCount = tables.reduce((n, t) => n + (t.columns || []).filter((c) => c.primary_key).length, 0);
  const fkCount = tables.reduce((n, t) => n + (t.columns || []).filter((c) => c.foreign_key).length, 0);

  return (
    <div>
      <div className="px-3.5 py-3 border-b border-stone-100 flex flex-wrap items-center gap-2">
        <span className="text-[12.5px] text-stone-600">
          <b className="font-semibold text-stone-800 tabular-nums">{tables.length}</b> tables from{" "}
          <span className="font-mono">{source}</span> ·{" "}
          <b className="font-semibold tabular-nums">{pkCount}</b> primary keys ·{" "}
          <b className="font-semibold tabular-nums">{fkCount}</b> foreign keys
        </span>
        <div className="ml-auto flex gap-2">
          <Button variant="secondary" size="sm" icon={RefreshCw} onClick={onReconnect}>Reconnect</Button>
          <Button variant="primary" size="sm" icon={Wand2} onClick={onPropose}>
            {proposed ? "Rebuild rules" : "Create rules from these tables"}
          </Button>
        </div>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="filter tables…"
          className="basis-full px-2.5 py-1.5 rounded-lg border border-stone-200 bg-white text-[12.5px]
                     focus:outline-none focus:ring-2 focus:ring-primary/30" />
      </div>
      <div className="overflow-auto max-h-[400px]">
        {shown.map((t) => (
          <div key={`${t.schema || ""}.${t.name}`} className="px-3.5 py-2.5 border-b border-stone-100 last:border-b-0">
            <h3 className="font-mono text-[12px] font-semibold text-stone-800 flex items-center gap-1.5">
              <Table2 size={12} className="text-stone-400" />{t.name}
              <span className="font-sans font-normal text-stone-500">
                {(t.columns || []).length} cols{typeof t.row_count === "number" ? ` · ${t.row_count.toLocaleString()} rows` : ""}
              </span>
            </h3>
            <div className="flex flex-wrap gap-1 mt-1.5">
              {(t.columns || []).map((c) => (
                <span key={c.name}
                  className={`font-mono text-[10.5px] px-1.5 py-0.5 rounded border inline-flex items-center gap-1
                    ${c.primary_key ? "border-amber-300 bg-amber-50 text-amber-800"
                      : c.foreign_key ? "border-teal-300 bg-teal-50 text-teal-800"
                      : "border-stone-200 bg-white text-stone-600"}`}
                  title={`${c.type || "?"}${c.references_table ? ` → ${c.references_table}` : ""}`}>
                  {c.primary_key && <KeyRound size={9} />}
                  {c.foreign_key && !c.primary_key && <Link2 size={9} />}
                  {c.name}
                </span>
              ))}
            </div>
          </div>
        ))}
        {!shown.length && <p className="px-3.5 py-6 text-sm text-stone-500">No table matches that filter.</p>}
      </div>
    </div>
  );
}
