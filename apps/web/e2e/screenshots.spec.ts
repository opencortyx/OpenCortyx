import { mkdirSync } from "node:fs";
import path from "node:path";
import { test, type Page } from "@playwright/test";
import { DASHBOARD_PAGES } from "./pages";

/** Regenerates the screenshots used in the documentation: `npm run screenshots`. */
const OUT_DIR = path.resolve(__dirname, "../../../docs/assets/screenshots");

test.beforeAll(() => mkdirSync(OUT_DIR, { recursive: true }));

/**
 * Run after the README demo take (`scripts/demo/record.mjs`), which leaves the workspace holding
 * `atlas-programme.json`: every page is then shown with that data, and the Assistant answers about it.
 */
const ASSISTANT_QUESTION = "Who leads the Payments Core Rewrite, and what is its biggest risk?";
const ASSISTANT_ANSWER = /Marek Nowak/;

/** The file the Add data page is shown with, its graph options open. */
const DRAFT_FILE = path.resolve(__dirname, "../scripts/demo/atlas-programme.json");

/**
 * Pages shown in use rather than in their opening state. Each step stays best-effort: on an empty
 * workspace there is nothing to open, and the page is captured as it is.
 */
const PREPARE: Record<string, (p: Page) => Promise<unknown>> = {
  "add-data": async (p) => {
    const [chooser] = await Promise.all([p.waitForEvent("filechooser"),
      p.getByText("Drop files here, or click to choose them").click()]);
    await chooser.setFiles(DRAFT_FILE);
    await p.getByText("Extract knowledge", { exact: true }).click({ timeout: 60_000 });
  },
  mapper: async (p) => {
    const project = p.locator('a[href^="/dashboard/mapper/"]').filter({ hasText: /^Project/ }).first();
    await (await project.count() ? project : p.locator('a[href^="/dashboard/mapper/"]').first()).click({ timeout: 15_000 });
    await p.waitForURL(/\/dashboard\/mapper\/.+/);
    await p.waitForLoadState("networkidle");
  },
  "nodes-relationships": async (p) => {
    await p.getByRole("button", { name: "Edit Project" }).click({ timeout: 15_000 });
    const panel = p.getByText(/When are two Project records the same thing\?/);
    await panel.waitFor({ timeout: 15_000 });
    await panel.scrollIntoViewIfNeeded();
  },
  "review-queue": (p) => p.getByRole("button", { name: /^Ontology/ }).click({ timeout: 15_000 }),
  evaluations: async (p) => {
    await p.getByRole("button", { name: /Run suite/ }).click({ timeout: 15_000 });
    await p.getByText(/Running case/).waitFor({ timeout: 15_000 });
    await p.getByText(/Running case/).waitFor({ state: "detached", timeout: 600_000 });
  },
  "custom-extractors": (p) => p.getByRole("tab", { name: /Built-in/ }).click({ timeout: 15_000 }),
};

for (const page of DASHBOARD_PAGES) {
  test(`screenshot ${page.slug}`, async ({ page: browser }) => {
    test.setTimeout(page.slug === "evaluations" || page.slug === "ai-assistant" ? 720_000 : 240_000);
    await browser.goto(page.path);
    await browser.waitForLoadState("networkidle");
    await PREPARE[page.slug]?.(browser).catch(() => undefined);
    if (page.slug === "ai-assistant") {
      await browser.getByPlaceholder("Ask a question...").fill(ASSISTANT_QUESTION);
      await browser.getByRole("button", { name: "Ask" }).click();
      // Without a model or the demo data there is no answer; the assistant is shown as it is.
      await browser.getByText(ASSISTANT_ANSWER).first().waitFor({ timeout: 600_000 }).catch(() => undefined);
    }
    await browser.waitForTimeout(page.slug === "lineage" ? 6000 : 2500);
    await browser.screenshot({ path: path.join(OUT_DIR, `${page.slug}.png`) });
  });
}

test("screenshot sign-in", async ({ browser }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto("/auth/login");
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: path.join(OUT_DIR, "sign-in.png") });
  await context.close();
});
