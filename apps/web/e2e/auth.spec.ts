import { expect, test } from "@playwright/test";

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("sign-in page shows the credentials form", async ({ page }) => {
    await page.goto("/auth/login");
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  test("has no session", async ({ request }) => {
    const session = await request.get("/api/auth/session");
    expect(await session.json()).toBeNull();
  });
});

test.describe("signed in", () => {
  test("session belongs to the admin", async ({ request }) => {
    const session = await (await request.get("/api/auth/session")).json();
    expect(session?.user?.email).toBe(process.env.OPENCORTYX_ADMIN_EMAIL);
    expect(session?.role).toBe("admin");
  });

  test("dashboard shows the navigation", async ({ page }) => {
    await page.goto("/dashboard/catalogue");
    for (const section of ["Assets", "AI Assistant", "Ingestion", "System Health"]) {
      await expect(page.getByRole("link", { name: section, exact: true }).first()).toBeVisible();
    }
  });
});
