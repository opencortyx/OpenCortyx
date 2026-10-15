import { expect, test } from "@playwright/test";
import { adminApi, deleteAsset, ingestDocument } from "./api";

const TAG = Date.now().toString(36);

test.describe("AI Assistant @llm", () => {
  let assetId = "";

  test.beforeAll(async () => {
    test.setTimeout(660_000);
    const api = await adminApi();
    ({ assetId } = await ingestDocument(api, `e2e-helios-${TAG}.md`,
      `# Project Helios ${TAG}\n\nProject Helios is a solar storage programme led by Mateo Alvarez.\n`));
    await api.dispose();
  });

  test.afterAll(async () => {
    if (!assetId) return;
    const api = await adminApi();
    await deleteAsset(api, assetId);
    await api.dispose();
  });

  test("answers a question from ingested data", async ({ page }) => {
    test.setTimeout(180_000);
    await page.goto("/dashboard/chat");
    const input = page.getByPlaceholder("Ask a question...");
    await input.fill(`Who leads Project Helios ${TAG}?`);
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText(/Mateo Alvarez|Alvarez/).first()).toBeVisible({ timeout: 150_000 });
  });
});
