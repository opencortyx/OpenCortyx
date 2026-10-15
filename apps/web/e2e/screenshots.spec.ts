import { mkdirSync } from "node:fs";
import path from "node:path";
import { test, type Page } from "@playwright/test";
import { DASHBOARD_PAGES } from "./pages";

/** Regenerates the screenshots used in the documentation: `npm run screenshots`. */
const OUT_DIR = path.resolve(__dirname, "../../../docs/assets/screenshots");

test.beforeAll(() => mkdirSync(OUT_DIR, { recursive: true }));

/** The AI Assistant is shown answering a question about the example data in `examples/data`. */
const ASSISTANT_QUESTION = "When do new employees enroll in health coverage?";

/** A file the Add data page drafts a reading for. */
const DRAFT_FILE = path.resolve(__dirname, "../scripts/demo/atlas-programme.json");

/**
 * Pages shown in use rather than in their opening state. Each step stays best-effort: on an empty
 * workspace there is nothing to open, and the page is captured as it is.
 */
const PREPARE: Record<string, (p: Page) => Promise<unknown>> = {
  "add-data": async (p) => {
    const [chooser] = await Promise.all([p.waitForEvent("filechooser"),
      p.getByText("Drop a file here, or click to choose one").click()]);
    await chooser.setFiles(DRAFT_FILE);
    await p.getByText(/Here is how it would read/).waitFor({ timeout: 180_000 });
  },
  mapper: async (p) => {
    await p.locator('a[href^="/dashboard/mapper/"]').first().click({ timeout: 15_000 });
    await p.waitForURL(/\/dashboard\/mapper\/.+/);
    await p.waitForLoadState("networkidle");
  },
  "nodes-relationships": async (p) => {
    await p.getByRole("button", { name: "Edit Lead" }).click({ timeout: 15_000 });
    const panel = p.getByText("When are two Lead records the same thing?");
    await panel.waitFor({ timeout: 15_000 });
    await panel.scrollIntoViewIfNeeded();
  },
  "review-queue": (p) => p.getByRole("button", { name: /^Ontology/ }).click({ timeout: 15_000 }),
  evaluations: async (p) => {
    await p.getByRole("button", { name: /Run suite/ }).click({ timeout: 15_000 });
    await p.getByText(/Running case/).waitFor({ timeout: 15_000 });
    await p.getByText(/Running case/).waitFor({ state: "detached", timeout: 600_000 });
  },
  "custom-metadata": (p) => p.getByRole("tab", { name: /Built-in/ }).click({ timeout: 15_000 }),
};

for (const page of DASHBOARD_PAGES) {
  test(`screenshot ${page.slug}`, async ({ page: browser }) => {
    test.setTimeout(page.slug === "evaluations" ? 720_000 : 240_000);
    await browser.goto(page.path);
    await browser.waitForLoadState("networkidle");
    await PREPARE[page.slug]?.(browser).catch(() => undefined);
    if (page.slug === "ai-assistant") {
      await browser.getByPlaceholder("Ask a question...").fill(ASSISTANT_QUESTION);
      await browser.getByRole("button", { name: "Ask" }).click();
      // Without a model or the example data there is no answer; the empty assistant is shown instead.
      await browser.getByText(/30 days/).first().waitFor({ timeout: 180_000 }).catch(() => undefined);
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
