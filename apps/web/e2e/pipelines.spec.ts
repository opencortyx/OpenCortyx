import { expect, test } from "@playwright/test";

/** Step 1 of the pipeline builder is the same reading rule for every kind: a mapping and two options. */
const KINDS = [
  { kind: "document", label: "Unstructured" },
  { kind: "structured", label: "Structured" },
  { kind: "semistructured", label: "Semi-structured" },
];

for (const { kind, label } of KINDS) {
  test(`${label} pipeline: step 1 is a mapping and two options`, async ({ page }) => {
    await page.goto(`/dashboard/pipelines/new?kind=${kind}`);
    await expect(page.getByRole("heading", { name: "How it reads" })).toBeVisible();
    // By role, not by text: during hydration the server-rendered copy and the client one are both
    // briefly in the DOM, and a strict text locator fails on the two matches rather than waiting.
    await expect(page.getByRole("heading", { name: "Mapping", exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Options" }).first()).toBeVisible();

    const accept = page.getByRole("checkbox", { name: /Read new kinds of file automatically/ });
    const extractOnly = page.getByRole("checkbox", { name: /Keep only what is extracted/ });
    // The heading above renders server-side, so it is visible before React has attached anything.
    // Wait for a control to be interactive before asserting on form state, or the assertions race
    // hydration and the failure looks like a missing element rather than a too-early read.
    await expect(accept).toBeEnabled();
    await expect(accept).toBeChecked();
    await expect(extractOnly).not.toBeChecked();
    await extractOnly.check();
    await expect(extractOnly).toBeChecked();

    for (const removed of ["Chunking", "Signature gate", "Tables", "Reading rules", "Target size", "Chunk node type"]) {
      await expect(page.getByText(removed, { exact: true })).toHaveCount(0);
    }
  });
}
