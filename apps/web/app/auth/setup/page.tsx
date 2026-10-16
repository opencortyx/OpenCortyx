"use client";
import { signIn } from "next-auth/react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { LogoMark } from "@/components/brand/Logo";
import { contextV2Api } from "@/lib/api/contextV2";

export default function SetupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [adoptDemo, setAdoptDemo] = useState(true);
  const [mode, setMode] = useState<"person" | "team">("person");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // If setup is already done, this page shouldn't show — go to login.
  useEffect(() => {
    contextV2Api.authNeedsSetup().then((r) => { if (r && !r.needs_setup) router.replace("/auth/login"); }).catch(() => {});
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password.length < 6) { setError("Password must be at least 6 characters."); return; }
    setBusy(true); setError("");
    try {
      await contextV2Api.authSetup(email, password, adoptDemo, { mode, name });   // creates the admin + workspace
      const res = await signIn("credentials", { email, password, redirect: false });
      if (res?.error) { setError("Created admin, but sign-in failed — try the login page."); setBusy(false); return; }
      router.push("/");
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Setup failed."); setBusy(false);
    }
  }

  const inputCls =
    "w-full h-11 px-3.5 rounded-lg border border-input bg-muted/40 text-sm transition-all " +
    "hover:border-foreground/20 focus:bg-background focus:outline-none focus:ring-4 focus:ring-primary/10 focus:border-primary";

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border border-black/5 bg-card px-8 pt-7 pb-8 space-y-6 shadow-[0_1px_2px_rgba(0,0,0,0.04),0_12px_40px_-12px_rgba(15,74,55,0.18)]"
        >
          <div className="flex flex-col items-center text-center">
            <LogoMark size={96} />
            <h1 className="mt-4 text-2xl font-semibold tracking-tight">Create your workspace</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              This account becomes the admin who invites everyone else.
            </p>
          </div>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <span className="text-sm font-medium">Whose workspace is this?</span>
              <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Workspace mode">
                {([["person", "Just me", "Your own knowledge"], ["team", "A team", "A team or organization"]] as const)
                  .map(([value, title, hint]) => (
                    <button key={value} type="button" role="radio" aria-checked={mode === value}
                      onClick={() => setMode(value)}
                      className={"rounded-lg border px-3 py-2 text-left text-sm transition-colors " +
                        (mode === value ? "border-primary bg-primary/5" : "border-input bg-muted/30 hover:border-foreground/20")}>
                      <span className="block font-medium">{title}</span>
                      <span className="block text-xs text-muted-foreground mt-0.5">{hint}</span>
                    </button>
                  ))}
              </div>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="ws-name" className="text-sm font-medium">
                {mode === "person" ? "Your name" : "Team or organization name"}
              </label>
              <input id="ws-name" type="text" maxLength={120} value={name}
                placeholder={mode === "person" ? "e.g. Ada Lovelace" : "e.g. Acme Analytics"}
                onChange={(e) => setName(e.target.value)} className={inputCls} />
              <p className="text-xs text-muted-foreground">The top of your graph. You can change it in Settings.</p>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="email" className="text-sm font-medium">Admin email</label>
              <input id="email" type="email" autoComplete="email" value={email}
                onChange={(e) => setEmail(e.target.value)} className={inputCls} required />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">Password</label>
              <input id="password" type="password" autoComplete="new-password" minLength={6} value={password}
                onChange={(e) => setPassword(e.target.value)} className={inputCls} required />
              <p className="text-xs text-muted-foreground">At least 6 characters.</p>
            </div>
            <label className="flex items-start gap-2.5 rounded-lg border border-input bg-muted/30 p-3 text-sm cursor-pointer hover:border-foreground/20 transition-colors">
              <input type="checkbox" checked={adoptDemo} onChange={(e) => setAdoptDemo(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-stone-300 accent-[hsl(var(--primary))]" />
              <span>
                <span className="font-medium">Keep existing data</span>
                <span className="block text-xs text-muted-foreground mt-0.5">
                  Documents and sessions already ingested. Uncheck to start empty.
                </span>
              </span>
            </label>
          </div>
          {error && (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 text-center">{error}</p>
          )}
          <button
            type="submit"
            disabled={busy}
            className="w-full h-11 rounded-lg bg-brand-gradient text-primary-foreground text-sm font-medium shadow-sm shadow-primary/30 transition-all hover:brightness-110 hover:shadow-md active:scale-[0.99] focus:outline-none focus-visible:ring-4 focus-visible:ring-primary/20 disabled:opacity-60"
          >
            {busy ? "Creating…" : "Create admin & continue"}
          </button>
        </form>
      </div>
    </div>
  );
}
