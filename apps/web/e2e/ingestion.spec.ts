import { expect, test } from "@playwright/test";

/** An auto-detect source chooses a pipeline for each detected kind, and can watch folders. */
test("auto-detect source offers a pipeline per kind and watch folders", async ({ page }) => {
  await page.goto("/dashboard/ingest");
  await page.getByRole("button", { name: "New Ingestion" }).click();
  await expect(page.getByRole("heading", { name: "New ingestion" })).toBeVisible();

  const classification = /^(Auto-detect|Force Structured|Force Semi-Structured|Force Unstructured)$/;
  await page.getByRole("button", { name: classification }).first().click();
  await page.getByRole("button", { name: "Auto-detect" }).last().click();

  await expect(page.getByText("Pipeline for each detected kind")).toBeVisible();
  for (const label of ["Pipeline for documents", "Pipeline for tables", "Pipeline for json and records"]) {
    await expect(page.getByLabel(label)).toBeVisible();
  }
  await expect(page.getByText("Watch folders", { exact: true })).toBeVisible();
  await expect(page.getByText("Bulk upload from this device")).toBeVisible();
});
