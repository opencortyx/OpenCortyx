import { mkdirSync } from "node:fs";
import { request } from "@playwright/test";
import { stateFor, tenant } from "./support";

/** Signs each of the tenant's roles in once (next-auth credentials flow) and saves the sessions. */
export default async function globalSetup() {
  const webURL = process.env.OPENCORTYX_WEB_URL ?? "http://localhost:3000";
  const t = tenant();
  mkdirSync("e2e/.auth", { recursive: true });
  for (const [role, { email, password }] of Object.entries(t.users)) {
    const web = await request.newContext({ baseURL: webURL });
    const { csrfToken } = await (await web.get("/api/auth/csrf")).json();
    const res = await web.post("/api/auth/callback/credentials", {
      form: { csrfToken, email, password, callbackUrl: `${webURL}/dashboard/catalogue` }, maxRedirects: 0,
    });
    const location = res.headers()["location"] ?? "";
    if (res.status() !== 302 || location.includes("error=")) throw new Error(`${role} sign-in failed: ${location}`);
    await web.storageState({ path: stateFor(role) });
    await web.dispose();
  }
}
