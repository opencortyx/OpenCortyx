import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { adminApi } from "./api";

/**
 * The Extractors page: one form for every extractor. Saving, listing, reopening and deleting go to
 * the real API; Try it's model call is answered here (the preview endpoint), so no model server is
 * needed - what is checked is what the page sends and how it shows the answer.
 */

const RUN = Date.now().toString(36);
const NAME = (s: string) => `e2e-${s}-${RUN}`;
let api: APIRequestContext;

test.beforeAll(async () => { api = await adminApi(); });
test.afterAll(async () => {
  const { templates } = await (await api.get("/api/v2/extraction-templates")).json();
  for (const t of templates.filter((x: any) => String(x.name).endsWith(RUN))) await api.delete(`/api/v2/extraction-templates/${t.id}`);
  await api.dispose();
});

const saved = async (name: string) => {
  const { templates } = await (await api.get("/api/v2/extraction-templates")).json();
  return templates.find((t: any) => t.name === name);
};

async function startFrom(page: Page, example: RegExp) {
  await page.goto("/dashboard/custom-assets");
  await page.getByRole("button", { name: example }).first().click();
  await expect(page.getByLabel("Extractor name")).toBeVisible();
}

async function nameIt(page: Page, name: string) {
  await page.getByLabel("Extractor name").fill(name);
}

test("an RDF extractor: the model names the kind, its fields and the relationship, each with an instruction", async ({ page }) => {
  await startFrom(page, /The model names everything \(RDF\)/);
  await nameIt(page, NAME("rdf"));
  await expect(page.getByText("What things to look for")).toBeVisible();
  await expect(page.getByText("What relationship to look for")).toBeVisible();
  await page.getByRole("button", { name: "Save extractor" }).click();
  await expect(page.getByRole("status")).toContainText(`Saved ${NAME("rdf")}`);

  const t = await saved(NAME("rdf"));
  expect(t.mechanism.ref).toBe("unstructured.ner");
  expect(t.mechanism.kinds).toEqual([expect.objectContaining({ type: "", fields_by_model: true, ask: expect.stringContaining("Parties") })]);
  expect(t.mechanism.links).toEqual([expect.objectContaining({ from: "~", to: "~", relation: "", ask: expect.stringContaining("Disputes") })]);
  expect(t.emit.edges[0]).toMatchObject({ over: "links", type: "$relation" });

  // reopened exactly as saved
  const row = page.getByRole("listitem").filter({ hasText: NAME("rdf") });
  await row.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByLabel("Extractor name")).toHaveValue(NAME("rdf"));
  await expect(page.getByPlaceholder("Parties, people, courts and places a contract or dispute names")).toHaveValue(/Parties/);

  // renamed: still one extractor
  await nameIt(page, NAME("rdf2"));
  await page.getByRole("button", { name: "Save extractor" }).click();
  await expect(page.getByRole("status")).toContainText(`Saved ${NAME("rdf2")}`);
  expect(await saved(NAME("rdf"))).toBeUndefined();
  expect((await saved(NAME("rdf2"))).id).toBe(t.id);

  // deleted after a confirmation that can be backed out of
  const renamed = page.getByRole("listitem").filter({ hasText: NAME("rdf2") });
  await renamed.getByRole("button", { name: `Delete ${NAME("rdf2")}` }).click();
  await renamed.getByRole("button", { name: "Keep" }).click();
  expect(await saved(NAME("rdf2"))).toBeDefined();
  await renamed.getByRole("button", { name: `Delete ${NAME("rdf2")}` }).click();
  await renamed.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("listitem").filter({ hasText: NAME("rdf2") })).toHaveCount(0);
  expect(await saved(NAME("rdf2"))).toBeUndefined();
});

test("Try it sends the unsaved form and shows things, connections and the input's fields", async ({ page }) => {
  let sent: any = null;
  await page.route("**/api/v2/extraction-templates/preview", async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      sample: sent.sample_text, error: null, warnings: [],
      nodes: [{ id: "Contract_0", type: "Contract", label: "MSA-2026", attributes: [{ key: "term", value: "24 months", filled: true }] },
              { id: "Organization_1", type: "Organization", label: "Acme Ltd", attributes: [] },
              { id: "Jurisdiction_2", type: "Jurisdiction", label: "England", attributes: [] }],
      edges: [{ rel: "HAS_PARTY", from: "MSA-2026", to: "Acme Ltd", from_id: "Contract_0", to_id: "Organization_1" },
              { rel: "MENTIONS", from: "Sample", to: "MSA-2026", from_id: "source", to_id: "Contract_0" }],
      source_attributes: [{ key: "risk_level", value: "low", filled: true }],
      counts: { nodes: 3, edges: 2, attributes: 2 },
    }) });
  });
  await startFrom(page, /A contract and its fields/);
  await page.getByRole("button", { name: "Run" }).click();
  const tryIt = page.getByRole("region", { name: "Try it" });
  await expect(tryIt.getByText("Things (3)")).toBeVisible();
  await expect(tryIt.getByText("term 24 months")).toBeVisible();
  await expect(tryIt.getByText("new kind")).toHaveCount(1);          // Jurisdiction: not one this form names
  await expect(tryIt.getByText("—HAS_PARTY→")).toBeVisible();
  await expect(tryIt.getByText("the input").first()).toBeVisible();
  await expect(tryIt.getByText("risk_level")).toBeVisible();
  expect(sent.mechanism.kinds.map((k: any) => k.type)).toEqual(["Contract", "Organization"]);
  expect(sent.mechanism.links[0]).toMatchObject({ from: "Contract", to: "Organization", relation: "HAS_PARTY" });
  expect(sent.mechanism.input_fields).toEqual([expect.objectContaining({ key: "risk_level" })]);

  // edited after the run: the result says it is out of date
  await page.getByLabel("Kind name").first().fill("Agreement");
  await expect(tryIt.getByText("Changed since this run")).toBeVisible();
});

test("nothing is saved without a name, under a taken name, or with a pattern that cannot run", async ({ page }) => {
  await api.post("/api/v2/extraction-templates", { data: { name: NAME("taken"), new: true,
    mechanism: { ref: "unstructured.dictionary", terms: ["Oslo"] }, emit: { nodes: [{ type: "Term", name: "$term" }] } } });
  await page.goto("/dashboard/custom-assets");
  await page.getByRole("button", { name: "New extractor" }).first().click();
  const save = page.getByRole("button", { name: "Save extractor" });
  const problems = page.locator('[role="alert"]:not(#__next-route-announcer__)');   // Next.js keeps an empty one

  await save.click();
  await expect(problems).toContainText("Give the extractor a name.");
  await nameIt(page, NAME("taken").toUpperCase());
  await save.click();
  await expect(problems).toContainText("already exists");

  await nameIt(page, NAME("pattern"));
  await page.getByRole("radio", { name: "Pattern" }).click();
  await page.getByLabel("Kind name").fill("Invoice");
  await page.getByLabel("Patterns").fill("INV-(\\d+");
  await save.click();
  await expect(problems).toContainText("not a valid regular expression");
  expect(await saved(NAME("pattern"))).toBeUndefined();

  // the model only decides connections between things it creates
  await page.getByRole("radio", { name: "Model" }).click();
  await expect(page.getByRole("radio", { name: "The model names it" }).last()).toBeDisabled();
});

test("Built-in lists what an upload can use, not the prompts that run at answer time", async ({ page }) => {
  await page.goto("/dashboard/custom-assets");
  await page.getByRole("tab", { name: /Built-in/ }).click();
  await expect(page.getByText("doc.trust", { exact: true })).toBeVisible();
  await expect(page.getByText("answer.generate", { exact: true })).toHaveCount(0);
  await expect(page.getByText("mem.summarizer", { exact: true })).toHaveCount(0);
});
