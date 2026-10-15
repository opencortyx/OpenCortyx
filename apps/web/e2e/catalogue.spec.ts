import { expect, test } from "@playwright/test";
import { adminApi, deleteAsset, ingestDocument } from "./api";

const TAG = Date.now().toString(36);
const FILE = `e2e-atlas-${TAG}.md`;

test.describe("Catalogue @ingest", () => {
  test.describe.configure({ mode: "serial" });
  let assetId = "";

  test.beforeAll(async () => {
    test.setTimeout(660_000);
    const api = await adminApi();
    ({ assetId } = await ingestDocument(api, FILE,
      `# Project Atlas ${TAG}\n\nProject Atlas is a logistics platform led by Priya Raman.\n`));
    await api.dispose();
  });

  test.afterAll(async () => {
    if (!assetId) return;
    const api = await adminApi();
    await deleteAsset(api, assetId);
    await api.dispose();
  });

  test("an ingested document appears in Assets", async ({ page }) => {
    await page.goto("/dashboard/catalogue");
    await page.getByPlaceholder("Filter by name, domain, format, fingerprint...").fill(FILE);
    await expect(page.getByText(FILE).first()).toBeVisible({ timeout: 30_000 });
  });

  test("its run is listed in Runs", async ({ page }) => {
    await page.goto("/dashboard/progress");
    await expect(page.getByText(FILE).first()).toBeVisible({ timeout: 30_000 });
  });
});
