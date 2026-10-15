import { APIRequestContext, expect, Page, request } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

/** The throwaway tenant this run signs in to (written by ui_tenant.py create). */
export type Tenant = { org: string; users: Record<"admin" | "editor" | "viewer", { email: string; password: string }>;
  api_key: string; fixtures: string[] };

export const TENANT_FILE = process.env.OPENCORTYX_UI_TENANT ?? "../api/test-results/dev/ui/tenant.json";
export const tenant = (): Tenant => JSON.parse(readFileSync(TENANT_FILE, "utf-8"));
export const fixture = (name: string) => join(dirname(TENANT_FILE), "fixtures", name);
export const stateFor = (role: string) => `e2e/.auth/regression-${role}.json`;
export const API_URL = process.env.OPENCORTYX_API_URL ?? "http://localhost:8000";

/** The API as the tenant's admin key — how every UI test checks what the page claims. */
export async function tenantApi(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: API_URL, extraHTTPHeaders: { "X-API-Key": tenant().api_key } });
}

/** Collects what goes wrong on a page while a test drives it: uncaught errors and API failures. */
export function watch(page: Page) {
  const problems: string[] = [];
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("response", (r) => {
    if (r.url().includes("/api/") && r.status() >= 500) problems.push(`${r.status()} ${r.request().method()} ${r.url()}`);
  });
  return problems;
}

/** Waits for a run of `asset` to finish and returns its outcome, from the API. */
export async function outcomeOf(api: APIRequestContext, asset: string, timeoutMs = 300_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const runs = (await (await api.get("/api/v2/progress", { params: { limit: 100 } })).json()).items ?? [];
    const run = runs.find((r: { asset: string }) => r.asset === asset);
    if (run && !["queued", "running"].includes(run.status)) {
      return (await api.get(`/api/v2/progress/${run.id}/outcome`)).json();
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error(`no finished run for ${asset} within ${timeoutMs} ms`);
}

export { expect };
