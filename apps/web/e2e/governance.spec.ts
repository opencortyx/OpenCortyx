import { APIRequestContext, expect, request, test } from "@playwright/test";
import { adminApi, deleteAsset, ingestDocument } from "./api";

/**
 * Cost, resolution quality, access and forgetting - through the UI a person uses and the API an
 * agent uses, refusals first.
 *
 * Access: two collections, a record in each, and a person whose group reads one of them. Every way
 * of reaching a record - by id, by search, by a question - must find the record they may read and
 * NOT the other. A workspace's policies are switched off again at the end, so the other specs (all
 * admin) are not affected by them while they run.
 */

const API_URL = process.env.OPENCORTYX_API_URL ?? "http://localhost:8000";
const run = Date.now().toString(36);

let api: APIRequestContext;
test.beforeAll(async () => { api = await adminApi(); });
test.afterAll(async () => { await api?.dispose(); });

async function signIn(email: string, password: string): Promise<APIRequestContext> {
  const login = await request.newContext({ baseURL: API_URL });
  const res = await login.post("/api/v2/auth/token", { data: { username: email, password } });
  expect(res.ok(), await res.text()).toBeTruthy();
  const { access_token } = await res.json();
  await login.dispose();
  return request.newContext({ baseURL: API_URL, extraHTTPHeaders: { Authorization: `Bearer ${access_token}` } });
}

/** Not found, as this API says it today: a 404, or a 200 whose body is {"status": "not_found"} (the
 *  known defect negative.spec.ts marks fixme - GET /nodes/{id} still answers a miss that way). */
async function absent(ctx: APIRequestContext, id: string): Promise<boolean> {
  const res = await ctx.get(`/api/v2/nodes/${id}`);
  if (res.status() === 404) return true;
  return res.ok() && (await res.json()).status === "not_found";
}

// ── API refusals ─────────────────────────────────────────────────────────────────────────────

test.describe("refusals", () => {
  test("forgetting needs the record's name typed exactly", async () => {
    const node = await (await api.post("/api/v2/nodes", { data: { type: "Note", label: `e2e-forget-${run}` } })).json();
    const wrong = await api.post("/api/v2/forget/subject", { data: { node_id: node.id, confirm: "something else", also_assets: [] } });
    expect(wrong.status()).toBe(422);
    expect(await absent(api, node.id), "a refused forget removes nothing").toBe(false);
    const right = await api.post("/api/v2/forget/subject", { data: { node_id: node.id, confirm: `E2E-FORGET-${run}`, also_assets: [] } });
    expect(right.status()).toBe(200);
    const job = await right.json();
    expect(job.status).toBe("done");
    expect(job.proof.remaining.graph).toBe(0);
    expect(await absent(api, node.id)).toBe(true);
  });

  test("unknown ids are not found, not errors", async () => {
    const zero = "00000000-0000-4000-8000-000000000000";
    expect((await api.get(`/api/v2/forget/subject/${zero}/preview`)).status()).toBe(404);
    expect((await api.get(`/api/v2/forget/jobs/${zero}`)).status()).toBe(404);
    expect((await api.post(`/api/v2/forget/jobs/${zero}/retry`)).status()).toBe(404);
    expect((await api.delete(`/api/v2/forget/tombstones/${zero}`)).status()).toBe(404);
    expect((await api.delete(`/api/v2/resolution/labels/${zero}`)).status()).toBe(404);
    expect((await api.delete(`/api/v2/access/policies/${zero}`)).status()).toBe(404);
    expect((await api.get(`/api/v2/resolution/masters/${zero}`)).status()).toBe(404);
  });

  test("resolution refuses impossible pairs", async () => {
    const a = await (await api.post("/api/v2/nodes", { data: { type: "Note", label: `e2e-a-${run}` } })).json();
    const b = await (await api.post("/api/v2/nodes", { data: { type: "Task", label: `e2e-b-${run}` } })).json();
    expect((await api.post("/api/v2/resolution/labels", { data: { a_id: a.id, b_id: a.id, same: true } })).status()).toBe(422);
    expect((await api.post("/api/v2/resolution/merge", { data: { a_id: a.id, b_id: b.id } })).status(), "two types").toBe(422);
    expect((await api.post("/api/v2/resolution/labels", { data: { a_id: "x", b_id: b.id, same: true } })).status()).toBe(422);
    expect((await api.post(`/api/v2/resolution/masters/${a.id}/split`, { data: { member_id: b.id } })).status()).toBe(404);
    expect((await api.post("/api/v2/resolution/quality/evaluate", { data: { node_type: "Note", overrides: { auto: "high" } } })).status()).toBe(422);
    for (const n of [a, b]) await api.delete(`/api/v2/nodes/${n.id}`);
  });

  test("a policy for a collection that does not exist is refused", async () => {
    const res = await api.post("/api/v2/access/policies", {
      data: { group: "nobody", collection_id: "00000000-0000-4000-8000-000000000000", max_sensitivity: "normal" } });
    expect(res.status()).toBe(404);
    expect((await api.post("/api/v2/access/policies", { data: { group: "  ", max_sensitivity: "normal" } })).status()).toBe(422);
    expect((await api.post("/api/v2/access/policies", { data: { group: "x", max_sensitivity: "secret" } })).status()).toBe(422);
  });

  test("a collection needs a name, and one name once", async () => {
    expect((await api.post("/api/v2/collections", { data: { name: "   " } })).status()).toBe(422);
    const c = await (await api.post("/api/v2/collections", { data: { name: `e2e-dup-${run}` } })).json();
    expect((await api.post("/api/v2/collections", { data: { name: `E2E-DUP-${run}` } })).status()).toBe(409);
    expect((await api.delete(`/api/v2/collections/${c.id}`)).status()).toBe(200);
  });

  test("a cost estimate without history says so instead of guessing", async () => {
    const res = await api.get("/api/v2/cost/estimate", { params: { purpose: `extract:never-run-${run}`, records: 1000 } });
    expect(res.status()).toBe(200);
    const e = await res.json();
    expect(e.estimate).toBeNull();
    expect(e.reason).toContain("preview");
    expect((await api.get("/api/v2/cost/estimate", { params: { purpose: "x", records: 0 } })).status()).toBe(422);
  });

  test("a non-admin cannot forget, set policies or clear the cache", async () => {
    const email = `e2e-editor-${run}@opencortyx.local`;
    const user = await (await api.post("/api/v2/users", { data: { email, password: "e2e-pass-1", role: "editor" } })).json();
    const editor = await signIn(email, "e2e-pass-1");
    expect((await editor.get("/api/v2/forget/jobs")).status()).toBe(403);
    expect((await editor.post("/api/v2/access/policies", { data: { group: "x", max_sensitivity: "normal" } })).status()).toBe(403);
    expect((await editor.delete("/api/v2/cost/cache")).status()).toBe(403);
    const n = await (await editor.post("/api/v2/nodes", { data: { type: "Note", label: `e2e-ed-${run}` } })).json();
    expect((await editor.delete(`/api/v2/assets/${n.id}`, { params: { forget: true } })).status(), "forgetting is admin-only").toBe(403);
    await api.delete(`/api/v2/nodes/${n.id}`);
    await editor.dispose();
    await api.delete(`/api/v2/users/${user.id}`);
  });
});

// ── access: one rule, every path ─────────────────────────────────────────────────────────────

test.describe("access control", () => {
  test("a person reads only what their group is given - by id, by search and in browsing", async () => {
    const sales = await (await api.post("/api/v2/collections", { data: { name: `e2e-sales-${run}` } })).json();
    const hr = await (await api.post("/api/v2/collections", { data: { name: `e2e-hr-${run}` } })).json();
    const deal = await (await api.post("/api/v2/nodes", { data: { type: "Note", label: `Orion deal ${run}`, collection_id: sales.id,
      properties: { text: "The Orion deal closes at 4.2 million euros." } } })).json();
    const pay = await (await api.post("/api/v2/nodes", { data: { type: "Note", label: `Mara Kell salary ${run}`, collection_id: hr.id,
      properties: { text: "Mara Kell earns 91,000 euros." } } })).json();
    const group = `e2e-sales-${run}`;
    const policy = await (await api.post("/api/v2/access/policies", { data: { group, collection_id: sales.id, max_sensitivity: "normal" } })).json();
    const email = `e2e-viewer-${run}@opencortyx.local`;
    const user = await (await api.post("/api/v2/users", { data: { email, password: "e2e-pass-2", role: "viewer" } })).json();
    expect((await api.patch(`/api/v2/users/${user.id}`, { data: { groups: [group.toUpperCase()] } })).status()).toBe(200);
    const lonely = `e2e-nogroup-${run}@opencortyx.local`;
    const other = await (await api.post("/api/v2/users", { data: { email: lonely, password: "e2e-pass-3", role: "viewer" } })).json();
    try {
      const viewer = await signIn(email, "e2e-pass-2");
      const nobody = await signIn(lonely, "e2e-pass-3");
      expect(await absent(viewer, deal.id), "their collection").toBe(false);
      expect(await absent(viewer, pay.id), "another collection is NOT FOUND, not forbidden").toBe(true);
      const found = await (await viewer.get("/api/v2/search", { params: { q: `salary ${run}` } })).json();
      expect(JSON.stringify(found)).not.toContain(pay.id);
      const mine = await (await viewer.get("/api/v2/search", { params: { q: `Orion deal ${run}` } })).json();
      expect(JSON.stringify(mine)).toContain(deal.id);
      expect(await absent(nobody, deal.id), "no policy names their group").toBe(true);
      expect(await absent(api, pay.id), "an admin reads every collection").toBe(false);
      const preview = await (await api.get("/api/v2/access/preview", { params: { user_id: user.id } })).json();
      expect(preview.collections).toEqual([`e2e-sales-${run}`]);
      await viewer.dispose(); await nobody.dispose();
    } finally {
      await api.delete(`/api/v2/access/policies/${policy.id}`);
      for (const n of [deal, pay]) await api.delete(`/api/v2/nodes/${n.id}`);
      for (const c of [sales, hr]) await api.delete(`/api/v2/collections/${c.id}`);
      for (const u of [user, other]) await api.delete(`/api/v2/users/${u.id}`);
    }
    const after = await (await api.get("/api/v2/access/policies")).json();
    expect(after.items.find((p: any) => p.id === policy.id)).toBeUndefined();
  });
});

// ── the pages ────────────────────────────────────────────────────────────────────────────────

test.describe("pages", () => {
  test("Settings reaches Cost & usage and Data deletion", async ({ page }) => {
    await page.goto("/dashboard/settings");
    await page.getByRole("link", { name: /Cost & usage/ }).click();
    await expect(page).toHaveURL(/\/dashboard\/cost/);
    await expect(page.getByRole("heading", { name: "Cost & usage" })).toBeVisible();
    await expect(page.getByText("Reading files: cost per 1,000 records")).toBeVisible();
    await page.goto("/dashboard/settings");
    await page.getByRole("link", { name: /Data deletion/ }).click();
    await expect(page.getByRole("heading", { name: "Data deletion" })).toBeVisible();
  });

  test("a budget alert outside 0..1 is refused with a message", async ({ page }) => {
    await page.goto("/dashboard/cost");
    const alert = page.getByLabel("Alert at (share of budget)");
    await alert.fill("2");
    await page.getByRole("button", { name: "Save budget" }).click();
    await expect(page.getByRole("status")).toContainText("between 0 and 1");
    await alert.fill("0.8");
    await page.getByRole("button", { name: "Save budget" }).click();
    await expect(page.getByRole("status")).toContainText("Budget saved");
  });

  test("forgetting stays disarmed until the name is typed exactly", async ({ page }) => {
    const label = `e2e-ui-forget-${run}`;
    const node = await (await api.post("/api/v2/nodes", { data: { type: "Note", label } })).json();
    await page.goto("/dashboard/forget");
    await page.getByLabel("Search records").fill("zz-no-such-record-zz");
    await expect(page.getByText("No record with that name.")).toBeVisible();
    await page.getByLabel("Search records").fill(label);
    await page.getByRole("button", { name: new RegExp(label) }).click();
    const go = page.getByRole("button", { name: /Forget everywhere/ });
    await expect(go).toBeDisabled();
    await page.getByLabel("Type the name to confirm").fill(label + "x");
    await expect(go).toBeDisabled();
    await page.getByLabel("Type the name to confirm").fill(label.toUpperCase());
    await expect(go).toBeEnabled();
    await go.click();
    await expect(page.getByRole("status")).toContainText("is forgotten");
    expect(await absent(api, node.id)).toBe(true);
  });

  test("Review has a Quality tab, and a bad import line keeps Import disabled", async ({ page }) => {
    await page.goto("/dashboard/review");
    await page.getByRole("link", { name: "Quality" }).click();
    await expect(page).toHaveURL(/\/dashboard\/review\/quality/);
    const box = page.getByLabel("Labelled pairs");
    await box.fill("Person,Ann Lee,Anne Lee,maybe");
    await expect(page.getByText(/"same" must be yes or no/)).toBeVisible();
    await expect(page.getByRole("button", { name: /^Import/ })).toBeDisabled();
    await box.fill(`E2ePerson${run},Ann Lee,Anne Lee,yes\nE2ePerson${run},Hans Andersen,Hans Anderson,no`);
    await page.getByRole("button", { name: /^Import 2/ }).click();
    await expect(page.getByRole("status")).toContainText("2 labelled pair(s) imported");
    await expect(page.getByRole("row", { name: new RegExp(`E2ePerson${run}`) })).toBeVisible();
  });

  test("Users & access turns access control on and off with a warning each way", async ({ page }) => {
    const group = `e2e-ui-${run}`;
    await page.goto("/dashboard/users");
    await expect(page.getByText(/^Access control is off:/)).toBeVisible();
    const add = page.getByRole("button", { name: "Add policy" });
    await expect(add, "no group, no policy").toBeDisabled();
    await page.getByRole("combobox", { name: "Group", exact: true }).fill(group);
    page.once("dialog", (d) => d.accept());
    await add.click();
    await expect(page.getByText(/Access control is on/).first()).toBeVisible();
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: `Remove the policy for ${group}` }).click();
    await expect(page.getByText(/^Access control is off:/)).toBeVisible();   // the banner, not the toast
  });

  test("a file deleted and forgotten is refused when it is uploaded again @ingest", async ({ page }) => {
    test.setTimeout(900_000);
    const name = `e2e-forgotten-${run}.md`;
    const text = `# Forgotten ${run}

The Vega project budget is 77,000 euros.`;
    const { assetId } = await ingestDocument(api, name, text);
    await page.goto("/dashboard/catalogue");
    await page.getByRole("button", { name: new RegExp(`^Delete .*${run}`) }).first().click();
    await expect(page.getByText("Also keep it from coming back.", { exact: false })).toBeVisible();
    await page.getByRole("checkbox").last().check();
    await page.getByPlaceholder("DELETE").fill("DELETE");
    await page.getByRole("button", { name: "Delete and forget" }).click();
    await expect(page.getByText(/^Deleted .* from every store .*nothing left\.$/)).toBeVisible({ timeout: 120_000 });
    expect(await absent(api, assetId)).toBe(true);
    const jobs = await (await api.get("/api/v2/forget/jobs")).json();
    const job = jobs.items.find((j: any) => j.target.asset_id === assetId);
    expect(job?.status).toBe("done");
    expect(Object.values(job.proof.remaining).every((v: any) => !v), JSON.stringify(job.proof)).toBeTruthy();
    // The same file again: the run fails, saying it was forgotten, and nothing comes back.
    const again = await api.post("/api/v2/sources/upload", {
      multipart: { file: { name, mimeType: "text/markdown", buffer: Buffer.from(text) } } });
    const { run_id } = await again.json();
    let status = "";
    for (let i = 0; i < 150 && !["completed", "failed", "canceled"].includes(status); i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const r = await (await api.get(`/api/v2/progress/${run_id}`)).json();
      status = r.status;
      if (status === "failed") expect(String(r.error)).toContain("forgotten");
    }
    expect(status).toBe("failed");
    expect(await absent(api, assetId)).toBe(true);
    // Lifting the tombstone lets it in again.
    const t = (await (await api.get("/api/v2/forget/tombstones")).json()).items.find((x: any) => x.job_id === job.id);
    expect((await api.delete(`/api/v2/forget/tombstones/${t.id}`)).status()).toBe(200);
    const back = await ingestDocument(api, name, text);
    expect(back.assetId).toBe(assetId);
    await deleteAsset(api, assetId);
  });
});
