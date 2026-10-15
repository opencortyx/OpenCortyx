"use client";
import { getCsrfToken, signIn } from "next-auth/react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { LogoMark } from "@/components/brand/Logo";
import { contextV2Api } from "@/lib/api/contextV2";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // First run with no users → send to the setup screen to create the admin.
  useEffect(() => {
    contextV2Api.authNeedsSetup().then((r) => { if (r?.needs_setup) router.replace("/auth/setup"); }).catch(() => {});
  }, [router]);

  // Set the CSRF cookie before the first submit: signIn posts a token the server checks against it,
  // and a submit that raced the cookie failed with MissingCSRF.
  useEffect(() => { getCsrfToken().catch(() => {}); }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    const attempt = () => signIn("credentials", { email, password, redirect: false });
    let res = await attempt();
    // Only CredentialsSignin means wrong credentials. Anything else (a MissingCSRF race) is retried
    // once with a fresh token, and never reported as "wrong password".
    if (res?.error && res.error !== "CredentialsSignin") res = await attempt();
    setBusy(false);
    if (res?.error === "CredentialsSignin") { setError("Invalid email or password."); return; }
    if (res?.error) { setError("Sign-in did not go through. Please try again."); return; }
    router.push("/");
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
            <h1 className="mt-4 text-2xl font-semibold tracking-tight">Welcome back</h1>
            <p className="mt-1 text-sm text-muted-foreground">Sign in to your workspace</p>
          </div>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="email" className="text-sm font-medium">Email</label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputCls}
                required
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">Password</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={inputCls}
                required
              />
            </div>
          </div>
          {error && (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 text-center">{error}</p>
          )}
          <button
            type="submit"
            disabled={busy}
            className="w-full h-11 rounded-lg bg-brand-gradient text-primary-foreground text-sm font-medium shadow-sm shadow-primary/30 transition-all hover:brightness-110 hover:shadow-md active:scale-[0.99] focus:outline-none focus-visible:ring-4 focus-visible:ring-primary/20 disabled:opacity-60"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
