import { mkdirSync } from "node:fs";
import { request } from "@playwright/test";

/**
 * Signs in once over HTTP (next-auth credentials flow) and saves the session for every test.
 * On a fresh installation it first completes the setup step that creates the admin account.
 */
export default async function globalSetup() {
  const webURL = process.env.OPENCORTYX_WEB_URL ?? "http://localhost:3000";
  const apiURL = process.env.OPENCORTYX_API_URL ?? "http://localhost:8000";
  const email = process.env.OPENCORTYX_ADMIN_EMAIL;
  const password = process.env.OPENCORTYX_ADMIN_PASSWORD;
  if (!email || !password) {
    throw new Error("Set OPENCORTYX_ADMIN_EMAIL and OPENCORTYX_ADMIN_PASSWORD to run the end-to-end tests.");
  }

  const api = await request.newContext({ baseURL: apiURL });
  const { needs_setup } = await (await api.get("/api/v2/auth/needs-setup")).json();
  if (needs_setup) {
    const setup = await api.post("/api/v2/auth/setup", { data: { email, password, adopt_demo: false } });
    if (!setup.ok()) throw new Error(`First-run setup failed: ${setup.status()} ${await setup.text()}`);
  }
  await api.dispose();

  const web = await request.newContext({ baseURL: webURL });
  const { csrfToken } = await (await web.get("/api/auth/csrf")).json();
  const signIn = await web.post("/api/auth/callback/credentials", {
    form: { csrfToken, email, password, callbackUrl: `${webURL}/dashboard/catalogue` },
    maxRedirects: 0,
  });
  const location = signIn.headers()["location"] ?? "";
  if (signIn.status() !== 302 || location.includes("error=")) {
    throw new Error(`Sign-in failed: ${signIn.status()} ${location}`);
  }

  mkdirSync("e2e/.auth", { recursive: true });
  await web.storageState({ path: "e2e/.auth/admin.json" });
  await web.dispose();
}
