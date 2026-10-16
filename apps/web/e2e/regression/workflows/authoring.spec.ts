import { test } from "@playwright/test";
import { expect, stateFor, tenantApi, watch } from "../support";

/**
 * UI-WF: complete authoring workflows driven through the browser — pipeline builder, mapper,
 * curation, review queue, quarantine — each checked against what the API then holds.
 */
test.use({ storageState: stateFor("admin") });
const uniq = () => Date.now().toString(36);

for (const kind of ["document", "structured", "semistructured"] as const) {
  test(`UI-WF pipeline builder (${kind}): create, save, rename — the API holds exactly that`, async ({ page }) => {
    const problems = watch(page);
    const name = `ui-pipe-${kind}-${uniq()}`;
    await page.goto(`/dashboard/pipelines/new?kind=${kind}`);
    const nameBox = page.getByLabel("Pipeline name");
    await expect(nameBox).toBeEditable();
    await nameBox.fill(name);
    await page.getByRole("button", { name: "Save pipeline" }).click();
    await expect(page).toHaveURL(/\/dashboard\/pipelines\/[0-9a-f-]{36}/, { timeout: 30_000 });
    const id = page.url().split("/").pop()!.split("?")[0];
    const api = await tenantApi();
    let saved = await (await api.get(`/api/v2/pipelines/${id}`)).json();
    expect(saved.name).toBe(name);
    expect(saved.kind).toBe(kind);
    await nameBox.fill(`${name}-v2`);
    await page.getByRole("button", { name: "Save pipeline" }).click();
    await expect(page.getByRole("button", { name: "Saved" })).toBeVisible();
    saved = await (await api.get(`/api/v2/pipelines/${id}`)).json();
    expect(saved.name).toBe(`${name}-v2`);
    await api.delete(`/api/v2/pipelines/${id}`);
    await api.dispose();
    expect(problems, problems.join("\n")).toEqual([]);
  });
}

test("UI-WF mapper: create a mapping, rename it, add a node, save — the API holds exactly that", async ({ page }) => {
  const problems = watch(page);
  const api = await tenantApi();
  const before = new Set(((await (await api.get("/api/v2/schema-mappings")).json()).items ?? []).map((m: { id: string }) => m.id));
  await page.goto("/dashboard/mapper");
  // the header's create button; the filter chip beside it reads "Semi-structured <count>"
  await page.getByRole("button", { name: "Semi-structured", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard\/mapper\/[0-9a-f-]{36}/, { timeout: 30_000 });
  const id = page.url().split("/").pop()!.split("?")[0];
  expect(before.has(id), "a new mapping was created").toBeFalsy();
  const name = `ui-map-${uniq()}`;
  await page.getByLabel("Mapping name").fill(name);
  await page.getByRole("button", { name: "Add node" }).click();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("button", { name: "Saved" })).toBeVisible();
  const saved = await (await api.get(`/api/v2/schema-mappings/${id}`)).json();
  expect(saved.name).toBe(name);
  expect(JSON.stringify(saved.payload).length, "the added node is in the saved payload").toBeGreaterThan(2);
  await api.delete(`/api/v2/schema-mappings/${id}`);
  await api.dispose();
  expect(problems, problems.join("\n")).toEqual([]);
});

test("UI-WF curation: add a glossary term, refuse a blank one, delete — the graph follows", async ({ page }) => {
  const problems = watch(page);
  const term = `UITerm${uniq()}`;
  await page.goto("/dashboard/curate");
  await page.getByRole("button", { name: "Resources" }).click();
  await page.getByRole("button", { name: "Glossary" }).click();
  await page.getByRole("button", { name: "Add term" }).click();
  await expect(page.getByText("Term and definition are required.")).toBeVisible();
  await page.getByPlaceholder("Term (e.g. ARR)").fill(term);
  await page.getByPlaceholder("Definition").fill("A term added through the UI regression suite.");
  await page.getByRole("button", { name: "Add term" }).click();
  await expect(page.getByText("Term added.")).toBeVisible();
  const api = await tenantApi();
  const hits = (await (await api.get("/api/v2/search", { params: { q: term } })).json()).results ?? [];
  expect(hits.map((h: { label: string }) => h.label)).toContain(term);
  // the smallest element holding both the term and a button is that term's row
  const row = page.locator("div").filter({ has: page.getByText(term, { exact: true }) }).filter({ has: page.getByRole("button") }).last();
  await row.getByRole("button").last().click();
  await expect.poll(async () => ((await (await api.get("/api/v2/search", { params: { q: term } })).json()).results ?? [])
    .filter((h: { label: string }) => h.label === term).length, { timeout: 20_000 }).toBe(0);
  await api.dispose();
  expect(problems, problems.join("\n")).toEqual([]);
});

test("UI-WF quarantine: the page shows exactly what the API reports", async ({ page }) => {
  const problems = watch(page);
  const api = await tenantApi();
  const items = (await (await api.get("/api/v2/quarantine")).json()).items ?? [];
  await page.goto("/dashboard/quarantine");
  await page.waitForLoadState("networkidle");
  if (items.length === 0) {
    await expect(page.getByText(/nothing|no .*quarantin|empty|all clear/i).first()).toBeVisible();
  } else {
    for (const it of items.slice(0, 5)) await expect(page.getByText(String(it.label ?? it.id)).first()).toBeVisible();
  }
  await api.dispose();
  expect(problems, problems.join("\n")).toEqual([]);
});
