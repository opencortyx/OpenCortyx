import { test } from "@playwright/test";
import { copyFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, fixture, outcomeOf, stateFor, tenantApi, watch } from "../support";

/**
 * UI-ADD: the full "Add data" workflow, UI → API → worker → graph → API → UI, for each kind of file.
 * The page's headline and counts must equal the outcome the API reports for the same run, and the
 * run must show as finished on the Runs page.
 */
const CASES = [
  { file: "records.json", expect: "found" }, { file: "records.jsonl", expect: "found" },
  { file: "nested.yaml", expect: "found" }, { file: "catalog.xml", expect: "found" },
  { file: "orders.csv", expect: "found" }, { file: "orders.tsv", expect: "found" },
  { file: "orders.xlsx", expect: "found" }, { file: "orders.parquet", expect: "found" },
  { file: "notes.md", expect: "any" }, { file: "notes.txt", expect: "any" },
  { file: "report.pdf", expect: "any" }, { file: "report.docx", expect: "any" },
  { file: "broken.json", expect: "failed" }, { file: "empty.csv", expect: "refused" },
];

test.use({ storageState: stateFor("admin") });

for (const c of CASES) {
  test(`UI-ADD ${c.file}: add data through the UI and see what the API says was produced`, async ({ page }) => {
    const problems = watch(page);
    const [stem, ext] = c.file.split(".");
    const name = `${stem}-${Date.now().toString(36)}.${ext}`;           // unique asset name per run
    const path = join(mkdtempSync(join(tmpdir(), "ui-")), name);
    copyFileSync(fixture(c.file), path);

    await page.goto("/dashboard/add");
    await page.locator('input[type="file"]').setInputFiles(path);
    const defaults = page.getByRole("button", { name: "Just add it with the defaults" });
    if (c.expect === "refused") {
      // An empty file must be refused with a message, and never become a run.
      await expect(page.getByText(/empty|no records|could not|nothing/i).first()).toBeVisible();
      const api = await tenantApi();
      const runs = (await (await api.get("/api/v2/progress", { params: { limit: 100 } })).json()).items ?? [];
      expect(runs.find((r: { asset: string }) => r.asset === name), "an empty file must not be queued").toBeUndefined();
      await api.dispose();
      return;
    }
    await expect(defaults).toBeEnabled({ timeout: 120_000 });   // documents are drafted with the model first
    await defaults.click();

    const api = await tenantApi();
    const outcome = await outcomeOf(api, name);
    if (c.expect === "failed") {
      expect(outcome.status).toBe("failed");
      await expect(page.getByText("This one did not go through")).toBeVisible();
    } else {
      expect(outcome.status, JSON.stringify(outcome).slice(0, 300)).toBe("completed");
      expect(outcome.total_nodes).toBeGreaterThan(0);
      for (const row of outcome.found ?? []) {
        // every kind the API reports is on the card, with the same count
        await expect(page.getByText(row.type, { exact: true }).first()).toBeVisible();
        await expect(page.getByText(row.count.toLocaleString("en-US"), { exact: true }).first()).toBeVisible();
      }
      if (c.expect === "found") expect((outcome.found ?? []).length).toBeGreaterThan(0);
    }

    await page.goto("/dashboard/progress");
    const card = page.locator("h3", { hasText: name });
    await expect(card).toBeVisible();
    await expect(card.locator("xpath=ancestor::div[contains(@class,'rounded-xl')][1]"))
      .toContainText(outcome.status === "completed" ? "100%" : /%/);
    await api.dispose();
    expect(problems, problems.join("\n")).toEqual([]);
  });
}
