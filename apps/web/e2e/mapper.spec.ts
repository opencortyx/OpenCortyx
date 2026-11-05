import { expect, test, type Route } from "@playwright/test";

/**
 * The Mapper page: one file, the kinds detection found in it, how they connect, one Save.
 *
 * The mapping API is answered here with LongMemEval's shape as detection read it (two kinds, the
 * second named after its identifier, its relationship named after it), so the test needs no upload
 * and no detection pass. What it checks is what the page sends: a rename made once reaches every
 * mapping that names the kind, and a relationship named after a kind follows its new name.
 */

const FILE = { id: "a1", name: "lme_e2e.json", kind: "semistructured" };
const SESSIONS = "Sessions (haystack_session_ids)";

const entity = (type: string, keys: string[], path: string, label: string) => ({
  type, keys, kind: "entity", path, label, assets: [{ id: FILE.id, name: FILE.name, fields: {} }], properties: [],
  materialize: true, status: "accepted", confidence: 1, rows: 0, attributes: [],
});
const MAPPINGS = [
  { id: "m1", name: "Question: rows", paradigm: "semistructured", source_type: "detected", status: "auto",
    knowledge: { version: 1, curated: false, entities: [entity("Question", ["question_id"], "", "question")],
      relationships: [{ name: "SESSIONS_HAYSTACK_SESSION_IDS", from_type: "Question", from_field: "haystack_session_ids",
                        to_type: SESSIONS, to_field: "session_id", origin: "inferred", coverage: 1, status: "accepted" }] } },
  { id: "m2", name: `${SESSIONS}: $.haystack_sessions[*]`, paradigm: "semistructured", source_type: "detected", status: "auto",
    knowledge: { version: 1, curated: false, entities: [entity(SESSIONS, ["session_id"], "$.haystack_sessions[*]", "")],
      relationships: [] } },
];
const field = (name: string, extra = {}) => ({ name, source: name, data_type: "text", filled: 6, distinct: 6, repeats: 0,
  words: 2, identifier: false, nameable: false, ...extra });
const SHAPE = [
  { asset_id: FILE.id, asset_name: FILE.name, kind: "semistructured", path: "", records: 6,
    fields: [field("question_id", { identifier: true }), field("question", { nameable: true, words: 9 }), field("haystack_session_ids")],
    rows: [{ question_id: "q1", question: "How many days ago did I buy the plant?", haystack_session_ids: "s1" }] },
  { asset_id: FILE.id, asset_name: FILE.name, kind: "semistructured", path: "$.haystack_sessions[*]", records: 9,
    fields: [field("session_id", { identifier: true }), field("date")], rows: [{ session_id: "s1", date: "2023/05/23" }] },
];

test("a rename is made once: the kind and the relationship named after it are saved under the new name", async ({ page }) => {
  const saved: Record<string, any> = {};
  await page.route("**/api/v2/schema-mappings**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.endsWith("/schema-mappings/assets")) return json({ items: [FILE] });
    if (url.pathname.endsWith("/schema-mappings/shape")) return json({ sources: SHAPE });
    if (url.pathname.endsWith("/validate")) return json({ valid: true, issues: [] });
    const put = url.pathname.match(/\/schema-mappings\/(m\d)\/knowledge$/);
    if (put && req.method() === "PUT") {
      saved[put[1]] = req.postDataJSON();
      return json({ ...MAPPINGS.find((m) => m.id === put[1]), knowledge: saved[put[1]].knowledge });
    }
    if (url.pathname.endsWith("/schema-mappings") && req.method() === "GET") return json({ items: MAPPINGS });
    return route.fallback();
  });

  await page.goto("/dashboard/mapper");
  await expect(page.getByRole("heading", { name: `What's in ${FILE.name}` })).toBeVisible();
  const names = page.getByLabel("Name of this kind");
  await expect(names).toHaveCount(2);
  const link = page.getByLabel("Relationship name");
  await expect(link).toHaveValue("SESSIONS_HAYSTACK_SESSION_IDS");

  await names.nth(1).fill("Conversation");
  await expect(link).toHaveValue("CONVERSATION");              // named after the kind: it follows
  await expect(page.getByText(`was ${SESSIONS}`)).toBeVisible();

  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/^Saved \d+ change/)).toBeVisible();

  expect(saved.m2.knowledge.entities[0].type).toBe("Conversation");
  const rel = saved.m1.knowledge.relationships[0];
  expect(rel).toMatchObject({ name: "CONVERSATION", from_type: "Question", to_type: "Conversation", to_field: "session_id" });
  expect(saved.m1.knowledge.entities[0].type, "the kind that was not renamed keeps its name").toBe("Question");
});

test("the old pages lead to the Mapper", async ({ page }) => {
  await page.goto("/dashboard/schema");
  await expect(page).toHaveURL(/\/dashboard\/mapper\?asset=documents/);
  await page.goto("/dashboard/json");
  await expect(page).toHaveURL(/\/dashboard\/add/);
});

test("a document shows how its layout was read, and its long paragraphs' split size reads it again", async ({ page }) => {
  const DOC = { id: "d1", name: "onboarding.md", kind: "unstructured" };
  const calls: string[] = [];
  await page.route("**/api/v2/schema-mappings**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.endsWith("/schema-mappings/assets")) return json({ items: [FILE, DOC] });
    if (url.pathname.endsWith("/schema-mappings/documents/d1")) return json({
      id: "d1", name: DOC.name, format: "md", filename: DOC.name,
      elements: [{ type: "Title", count: 2 }, { type: "Text", count: 3 }],
      sections: [{ title: "Onboarding", level: 0, items: 1 }, { title: "Benefits", level: 0, items: 2 }],
      outside_sections: 0, passages: 3, longest: 790, average: 400, split_at: 800,
      samples: ["New hires complete IT setup on day one."], found: [{ type: "Person", count: 2 }], can_read_again: true });
    if (url.pathname.endsWith("/documents/d1/read-again")) { calls.push("read-again"); return json({ task_id: "t", run_id: "r" }); }
    if (url.pathname.endsWith("/files/d1/extract")) { calls.push(`extract:${req.postDataJSON().extractor}`); return json({ task_id: "t" }); }
    if (url.pathname.endsWith("/schema-mappings") && req.method() === "GET") return json({ items: MAPPINGS });
    return route.fallback();
  });
  await page.route("**/api/v2/config", async (route: Route) => {
    if (route.request().method() === "PUT") calls.push(`split:${route.request().postDataJSON().values.chunk_window_chars}`);
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/v2/extraction-templates", (route: Route) => route.fulfill({ status: 200, contentType: "application/json",
    body: JSON.stringify({ templates: [{ name: "document_entities", executable: true, category: "unstructured", mechanism: { ref: "unstructured.ner" } }] }) }));

  await page.goto("/dashboard/mapper?asset=d1");
  await expect(page.getByRole("heading", { name: "How onboarding.md was read" })).toBeVisible();
  await expect(page.getByText("Section headings").or(page.getByText("Titles"))).toBeVisible();
  await expect(page.getByText("Benefits", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Kinds extractors found in this file")).toContainText("Person");

  await page.getByLabel(/Split paragraphs longer than/).fill("300");
  await page.getByRole("button", { name: "Save and read this document again" }).click();
  await expect(page.getByText(/Reading onboarding.md again with paragraphs split at 300/)).toBeVisible();

  await page.getByLabel("Run an extractor on this file").selectOption("document_entities");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText(/Running over onboarding.md/)).toBeVisible();
  expect(calls).toEqual(["split:300", "read-again", "extract:document_entities"]);
});
