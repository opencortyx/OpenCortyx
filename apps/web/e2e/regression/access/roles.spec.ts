import { request, test } from "@playwright/test";
import { copyFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { API_URL, expect, fixture, stateFor, tenant, tenantApi } from "../support";

/** UI-ACC: what each role can do through the UI is exactly what the API allows it to do. */

test.describe("viewer", () => {
  test.use({ storageState: stateFor("viewer") });

  test("UI-ACC viewer: adding data is refused and nothing is ingested", async ({ page }) => {
    const name = `viewer-${Date.now().toString(36)}.json`;
    const path = join(mkdtempSync(join(tmpdir(), "ui-")), name);
    copyFileSync(fixture("records.json"), path);
    await page.goto("/dashboard/add");
    await page.locator('input[type="file"]').setInputFiles(path);
    const add = page.getByRole("button", { name: "Just add it with the defaults" });
    await expect(add).toBeEnabled({ timeout: 120_000 });   // reading drafts first; wait for the choice
    await add.click();
    await expect(page.getByText(/read-only|not allowed|permission|cannot modify|forbidden/i).first()).toBeVisible();
    const api = await tenantApi();
    const runs = (await (await api.get("/api/v2/progress", { params: { limit: 100 } })).json()).items ?? [];
    expect(runs.find((r: { asset: string }) => r.asset === name), "a viewer's upload must not become a run").toBeUndefined();
    await api.dispose();
  });
});

test.describe("sign-in", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("UI-ACC sign-in page: wrong password stays on sign-in with an error", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel(/email/i).fill(tenant().users.editor.email);
    await page.getByLabel(/password/i).fill("definitely-wrong");
    await page.getByRole("button", { name: /sign in|log in/i }).click();
    await expect(page).toHaveURL(/\/auth\/login/);
    await expect(page.getByText(/invalid|incorrect|wrong|failed/i).first()).toBeVisible();
  });

  for (const role of ["admin", "editor", "viewer"] as const) {
    test(`UI-ACC sign-in page: ${role} signs in and lands on the dashboard with that role`, async ({ page }) => {
      const u = tenant().users[role];
      await page.goto("/auth/login");
      await page.getByLabel(/email/i).fill(u.email);
      await page.getByLabel(/password/i).fill(u.password);
      await page.getByRole("button", { name: /sign in|log in/i }).click();
      await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 });
      const api = await request.newContext({ baseURL: API_URL });
      const tok = (await (await api.post("/api/v2/auth/token", { data: { username: u.email, password: u.password } })).json()).access_token;
      const me = await (await api.get("/api/v2/auth/me", { headers: { Authorization: `Bearer ${tok}` } })).json();
      expect(me.role).toBe(role);
      await api.dispose();
    });
  }
});

test.describe("admin", () => {
  test.use({ storageState: stateFor("admin") });

  test("UI-ACC admin: the Users page lists exactly the tenant's accounts from the API", async ({ page }) => {
    await page.goto("/dashboard/users");
    const api = await tenantApi();
    const users = (await (await api.get("/api/v2/users")).json()).items ?? [];
    expect(users.length).toBeGreaterThanOrEqual(3);
    for (const u of users) await expect(page.getByText(u.email).first()).toBeVisible();
    await api.dispose();
  });
});
