import { test } from "@playwright/test";
import { DASHBOARD_PAGES } from "../../pages";
import { expect, stateFor, watch } from "../support";

/**
 * UI-NAV: every dashboard page × every role renders its own content with no uncaught page error, no
 * API 5xx while loading, and no error screen — the floor every workflow below stands on.
 */
for (const role of ["admin", "editor", "viewer"] as const) {
  test.describe(`as ${role}`, () => {
    test.use({ storageState: stateFor(role) });
    for (const p of DASHBOARD_PAGES) {
      test(`UI-NAV ${role} ${p.slug}: ${p.name} renders`, async ({ page }) => {
        const problems = watch(page);
        await page.goto(p.path);
        await expect(page).toHaveURL(new RegExp(p.path.replace(/\//g, "\\/")));
        await page.waitForLoadState("networkidle");
        await expect(page.getByText(/Application error|Something went wrong|Unhandled Runtime Error/i)).toHaveCount(0);
        await expect(page.locator("main")).toBeVisible();
        expect(problems, problems.join("\n")).toEqual([]);
      });
    }
  });
}
