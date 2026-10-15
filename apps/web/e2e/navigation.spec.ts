import { expect, test } from "@playwright/test";
import { DASHBOARD_PAGES } from "./pages";

const CRASH_TEXT = /Application error|Unhandled Runtime Error|Something went wrong|This page could not be found/i;

for (const page of DASHBOARD_PAGES) {
  test(`${page.section} › ${page.name} loads without errors`, async ({ page: browser }) => {
    const pageErrors: string[] = [];
    browser.on("pageerror", (error) => pageErrors.push(error.message));

    const response = await browser.goto(page.path);
    expect(response?.status(), "document status").toBeLessThan(400);

    await expect(browser).toHaveURL(new RegExp(`${page.path}(\\?|$)`));
    await browser.waitForLoadState("networkidle");
    await expect(browser.locator("body")).not.toContainText(CRASH_TEXT);
    await expect(browser.getByRole("link", { name: page.name, exact: true }).first()).toBeVisible();

    expect(pageErrors, "uncaught errors in the browser").toEqual([]);
  });
}
