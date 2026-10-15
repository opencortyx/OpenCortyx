import { APIRequestContext, expect, request, test } from "@playwright/test";
import { adminApi } from "./api";

/**
 * The failures, rather than the successes.
 *
 * Every other spec here proves the product works when used correctly. These prove it refuses
 * correctly when it is not — the half that rots silently, because nothing in normal use exercises
 * it. A 500 where a 401 belongs, or a 200 where a 404 belongs, is a bug a happy-path suite will
 * never see.
 *
 * Each test asserts the SPECIFIC status the contract promises, never merely "not ok": an endpoint
 * that answers 500 to bad input is broken in a way `expect(res.ok()).toBeFalsy()` would pass.
 *
 * NB the API authenticates with a Bearer token, while Playwright's built-in `request` fixture
 * carries the web app's next-auth cookie. Anything hitting :8000 therefore goes through
 * `adminApi()`; using the fixture makes every call an accidental 401 and the test proves nothing.
 */

const API_URL = process.env.OPENCORTYX_API_URL ?? "http://localhost:8000";

/** A read endpoint that exists, is authenticated, and is cheap. */
const GUARDED = "/api/v2/stats";

let api: APIRequestContext;
test.beforeAll(async () => { api = await adminApi(); });
test.afterAll(async () => { await api?.dispose(); });

test.describe("authentication is required", () => {
  test("a guarded endpoint rejects a request with no token", async () => {
    const anon = await request.newContext({ baseURL: API_URL });
    const res = await anon.get(GUARDED);
    expect(res.status(), "no token must be 401, not 200 and not 500").toBe(401);
    await anon.dispose();
  });

  test("a guarded endpoint rejects a forged token", async () => {
    const forged = await request.newContext({
      baseURL: API_URL,
      extraHTTPHeaders: { Authorization: "Bearer not.a.real.token" },
    });
    expect((await forged.get(GUARDED)).status()).toBe(401);
    await forged.dispose();
  });

  test("the same endpoint answers a real token", async () => {
    // Without this, the two tests above would also pass against an endpoint that is simply broken.
    expect((await api.get(GUARDED)).status()).toBe(200);
  });

  test("the wrong password is refused", async () => {
    const anon = await request.newContext({ baseURL: API_URL });
    const res = await anon.post("/api/v2/auth/token", {
      data: { username: process.env.OPENCORTYX_ADMIN_EMAIL, password: "definitely-not-the-password" },
    });
    expect(res.status()).toBe(401);
    // The body must not distinguish "no such user" from "wrong password", or it enumerates accounts.
    expect((await res.text()).toLowerCase()).not.toContain("not found");
    await anon.dispose();
  });

  test("an unknown account is refused the same way as a wrong password", async () => {
    const anon = await request.newContext({ baseURL: API_URL });
    const res = await anon.post("/api/v2/auth/token", {
      data: { username: "nobody@example.invalid", password: "whatever" },
    });
    expect(res.status()).toBe(401);
    await anon.dispose();
  });
});

test.describe("first-run setup cannot be replayed", () => {
  test("setup is closed once an admin exists", async () => {
    const anon = await request.newContext({ baseURL: API_URL });
    const { needs_setup } = await (await anon.get("/api/v2/auth/needs-setup")).json();
    expect(needs_setup, "global setup already created the admin").toBe(false);

    const res = await anon.post("/api/v2/auth/setup", {
      data: { email: "intruder@example.invalid", password: "hunter2hunter2", adopt_demo: false },
    });
    expect(res.status(), "a second setup call must be refused, or anyone can mint an admin").toBe(409);
    await anon.dispose();
  });
});

test.describe("bad input is rejected, not absorbed", () => {
  test("a malformed body gets 422 rather than a 500", async () => {
    expect((await api.post("/api/v2/context", { data: { not_a_query: 123 } })).status()).toBe(422);
  });

  test("an unparseable node id is rejected before it reaches a store", async () => {
    const res = await api.get("/api/v2/nodes/this-is-not-a-uuid");
    expect(res.status()).toBe(422);
    expect(await res.text()).not.toContain("Traceback");
  });

  test("an empty upload does not become an ingestion run", async () => {
    const res = await api.post("/api/v2/sources/upload", {
      multipart: { file: { name: "empty.md", mimeType: "text/markdown", buffer: Buffer.from("") } },
    });
    expect(res.status(), "an empty file must be refused, not queued").toBeGreaterThanOrEqual(400);
    expect(res.status(), "and refused as the caller's fault, not ours").toBeLessThan(500);
  });
});

test.describe("internals are not exposed", () => {
  test("an error body carries no traceback or driver detail", async () => {
    const body = (await (await api.get("/api/v2/nodes/not-a-uuid")).text()).toLowerCase();
    for (const leak of ["traceback", "site-packages", "sqlalchemy", "psycopg", "asyncpg"]) {
      expect(body, `error responses must not leak ${leak}`).not.toContain(leak);
    }
  });

  test("health does not echo secrets", async () => {
    const res = await api.get("/api/v2/health");
    expect(res.ok()).toBeTruthy();
    const body = (await res.text()).toLowerCase();
    for (const secret of ["jwt_secret", "password", "api_key"]) {
      expect(body, `health must not echo ${secret}`).not.toContain(`"${secret}"`);
    }
  });
});

/**
 * KNOWN DEFECT, deliberately not asserted as passing.
 *
 * `GET /nodes/{id}` answers 200 with `{"status": "not_found"}` when the node does not exist, so
 * `res.ok()` is true for a miss and every client has to read the body to find out. The codebase
 * already treats this as a defect — see the comment at api/routes/proposals.py, which calls it
 * "the same failure-read-as-success" — but roughly ten endpoints still do it, and two frontend
 * components (catalogue/[id]/page.tsx and MappingVerifyPanel.tsx) branch on the 200 body, so the
 * fix is an API change with consumers, not a one-line edit.
 *
 * Left as `fixme` rather than deleted or rewritten to assert 200: writing it green would bless the
 * behaviour and remove the only marker that it is wrong.
 */
test.fixme("a well-formed id that does not exist is a 404", async () => {
  const res = await api.get("/api/v2/nodes/00000000-0000-4000-8000-000000000000");
  expect(res.status()).toBe(404);
});

test.describe("the UI refuses gracefully", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a signed-out visitor to the dashboard lands on sign-in", async ({ page }) => {
    await page.goto("/dashboard/catalogue");
    // toHaveURL polls the current URL; waitForURL waits on a navigation EVENT, and the client-side
    // redirect aborts the in-flight one ("net::ERR_ABORTED; maybe frame was detached?"). Assert the
    // state you want, not the transition you think gets you there.
    await expect(page).toHaveURL(/\/auth\/login/, { timeout: 15_000 });
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  });

  test("a wrong password shows an error and does not sign in", async ({ page }) => {
    await page.goto("/auth/login");
    await page.getByLabel("Email").fill(process.env.OPENCORTYX_ADMIN_EMAIL ?? "admin@example.com");
    await page.getByLabel("Password").fill("definitely-not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    // It must stay on sign-in; reaching the dashboard would mean the check did nothing.
    await expect(page).toHaveURL(/\/auth\/login/, { timeout: 15_000 });
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  });
});
