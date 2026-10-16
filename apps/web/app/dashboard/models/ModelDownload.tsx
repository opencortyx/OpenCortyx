"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, HardDriveDownload, Loader2, Trash2 } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, DiscoveredModel, HFDownloadStatus, HFModelInfo } from "@/lib/api/contextV2";

/**
 * Inside the Add-model form: fetch the Model ID the form names when it is not on this machine yet,
 * for any role.
 *
 *   - `transformers` (generation, extraction, utility): a Hugging Face model run by OpenCortyx on
 *     the CPU. The API downloads its safetensors weights into the shared model cache.
 *   - `fastembed` (embedding, rerank): an ONNX model OpenCortyx runs on the CPU, from fastembed's
 *     catalogue. Downloaded now rather than on its first use.
 *   - `openai_compatible` pointing at Ollama: Ollama pulls the model itself, a GGUF repository from
 *     Hugging Face (`hf.co/<repo>`) or an Ollama name. Fast, on the GPU.
 *
 * The form saves the row as usual afterwards; this only puts the files where the row expects them.
 */
// The API container's memory limit by default (compose.yaml, API_MEM_LIMIT): a model bigger than
// this cannot be loaded there next to the API itself.
const API_MEMORY = 3 * 1024 ** 3;

export const DOWNLOADABLE = new Set(["transformers", "fastembed", "openai_compatible"]);

export default function ModelDownload({ provider, role, baseUrl, modelId, available, onDone, onBusy }: {
  provider: string;
  role: string;
  baseUrl: string;
  modelId: string;
  available: DiscoveredModel[];              // what the picker lists: downloaded / served / supported
  onDone: (modelId: string) => void;         // the downloaded model's ID, as the row must name it
  onBusy: (busy: boolean) => void;
}) {
  const [token, setToken] = useState("");
  const [info, setInfo] = useState<HFModelInfo | null>(null);
  const [status, setStatus] = useState<HFDownloadStatus | null>(null);
  const [pull, setPull] = useState<{ text: string; completed?: number; total?: number } | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const id = modelId.trim();
  const ollama = provider === "openai_compatible";
  const ollamaId = (n: string) => ((n.split("/").pop() || "").includes(":") ? n : `${n}:latest`);
  const ollamaName = (n: string) => (n.includes("/") && !n.startsWith("hf.co/") ? `hf.co/${n.replace(/^https?:\/\/huggingface\.co\//, "")}` : n);
  const listed = available.find((m) => m.id.toLowerCase() === id.toLowerCase());
  const have = provider === "fastembed" ? !!listed?.downloaded
    : ollama ? !!listed || available.some((m) => m.id === ollamaId(ollamaName(id)))
    : !!listed;

  useEffect(() => { setInfo(null); setStatus(null); setPull(null); setError(""); }, [id, provider, role]);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);
  const busy = (v: boolean) => { setWorking(v); onBusy(v); };
  const detail = (e: any) => e?.response?.data?.detail || e?.message || String(e);

  // Poll a download running in the API until it is done or failed.
  const follow = (read: () => Promise<HFDownloadStatus>, name: string) => {
    if (poll.current) clearInterval(poll.current);
    poll.current = setInterval(async () => {
      try {
        const s = await read();
        setStatus((prev) => ({ ...s, total_bytes: s.total_bytes ?? prev?.total_bytes ?? null }));
        if (s.status === "done" || s.status === "failed") {
          if (poll.current) clearInterval(poll.current);
          busy(false);
          if (s.status === "done") onDone(name);
          else setError(s.error || "The download failed.");
        }
      } catch { /* a missed poll is retried on the next tick */ }
    }, 2000);
  };

  const downloadHere = async () => {
    setError(""); busy(true);
    try {
      if (provider === "fastembed") {
        const name = listed?.id || id;
        setStatus(await contextV2Api.fastembedDownload(name, role));
        follow(() => contextV2Api.fastembedDownloadStatus(name, role), name);
        return;
      }
      const meta = await contextV2Api.hfModelInfo(id, token);
      setInfo(meta);
      if (!meta.runs_in_opencortyx) { busy(false); return; }
      setStatus(await contextV2Api.hfDownload(id, token));
      follow(() => contextV2Api.hfDownloadStatus(meta.repo), meta.repo);
    } catch (e) { setError(detail(e)); busy(false); }
  };

  const pullIntoOllama = async () => {
    setError(""); busy(true); setPull({ text: "Connecting to Ollama…" });
    let name = ""; let failed = "";
    try {
      await contextV2Api.ollamaPull(baseUrl, id, (ev) => {
        if (ev.model) name = ev.model;
        if (ev.error) { failed = ev.error; return; }
        setPull({ text: ev.status || "", completed: ev.completed, total: ev.total });
      });
    } catch (e) { failed = detail(e); }
    busy(false); setPull(null);
    if (failed) { setError(failed); return; }
    onDone(ollamaId(name));      // Ollama lists an untagged model as `<name>:latest`
  };

  const removeFiles = async () => {
    if (!confirm(`Delete the downloaded files of ${id}? A model that uses it stops working.`)) return;
    try { await contextV2Api.hfDeleteLocal(id); onDone(""); } catch (e) { setError(detail(e)); }
  };

  if (!id) return null;
  if (have) {
    return ollama ? null : (
      <p className="text-[11px] text-emerald-700 -mt-2 flex items-center gap-1.5">
        <CheckCircle2 size={12} /> Downloaded to OpenCortyx.
        {provider === "transformers" && (
          <button type="button" onClick={removeFiles} className="ml-auto text-stone-400 hover:text-red-600 flex items-center gap-1"><Trash2 size={11} /> Delete files</button>
        )}
      </p>
    );
  }
  if (provider === "fastembed" && !listed) {
    return <p className="text-[11px] text-amber-700 -mt-2">fastembed cannot run this model. Pick one from the list above.</p>;
  }

  const pct = (d?: number | null, t?: number | null) => (t ? Math.min(100, Math.round(((d || 0) / t) * 100)) : null);
  const progress = ollama ? pct(pull?.completed, pull?.total) : pct(status?.done_bytes, status?.total_bytes);

  return (
    <div className="border border-stone-200 rounded-xl p-3 bg-stone-50/60 space-y-2 -mt-1">
      <p className="text-xs text-stone-600">
        {provider === "transformers"
          ? <>Not downloaded yet. OpenCortyx downloads it from Hugging Face and runs it on the CPU: keep to small instruct models (0.5B to 1.5B).</>
          : provider === "fastembed"
          ? <>Not downloaded yet ({fmtBytes(listed?.size)}). OpenCortyx runs it on the CPU; download it now so the first {role === "rerank" ? "search" : "ingest"} does not wait for it.</>
          : <>This server does not have it. If it is Ollama, it can download it: a Hugging Face GGUF repository (add <span className="font-mono">:Q4_K_M</span> to pick a quantization) or an Ollama name. Keep this open while it downloads.</>}
      </p>
      {provider === "transformers" && (
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} disabled={working}
          aria-label="Hugging Face token" placeholder="Hugging Face token, only for gated models (not stored)"
          className="w-full border border-stone-200 rounded-lg px-3 py-1.5 text-xs bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
      )}
      {info && (
        <p className={`text-xs ${info.runs_in_opencortyx ? "text-stone-600" : "text-red-700"}`}>
          {info.runs_in_opencortyx
            ? <>Download {fmtBytes(info.bytes)} · needs about {fmtBytes(info.memory_bytes)} of memory in each service that uses it.</>
            : <>It cannot run in OpenCortyx: {info.why_not}.</>}
          {info.runs_in_opencortyx && info.memory_bytes > API_MEMORY && (
            <span className="block text-amber-700">More than the API&apos;s 3 GB memory limit: choose a smaller model, use Ollama, or raise API_MEM_LIMIT in .env.</span>
          )}
        </p>
      )}
      {working && (status?.status === "downloading" || pull) && (
        <div className="space-y-1">
          <p className="text-xs text-stone-600 flex items-center gap-1.5">
            <Loader2 size={12} className="animate-spin" />
            {ollama ? (pull?.text || "Downloading…") : `Downloading… ${fmtBytes(status?.done_bytes)} of ${fmtBytes(status?.total_bytes)}`}
          </p>
          {progress !== null && (
            <div className="h-1.5 rounded-full bg-stone-200 overflow-hidden" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}>
              <div className="h-full bg-emerald-500 transition-all" style={{ width: `${progress}%` }} />
            </div>
          )}
        </div>
      )}
      {error && <p className="text-xs text-red-700 flex items-start gap-1.5"><AlertCircle size={13} className="shrink-0 mt-px" /> {error}</p>}
      <Button variant="secondary" size="sm" icon={HardDriveDownload} busy={working} disabled={working}
        onClick={ollama ? pullIntoOllama : downloadHere}>
        {ollama ? "Download into Ollama" : "Download"}
      </Button>
    </div>
  );
}

function fmtBytes(n?: number | null) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
