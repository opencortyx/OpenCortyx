import { APIRequestContext, expect, request } from "@playwright/test";

const API_URL = process.env.OPENCORTYX_API_URL ?? "http://localhost:8000";

/** An API client signed in as the admin used by the end-to-end tests. */
export async function adminApi(): Promise<APIRequestContext> {
  const login = await request.newContext({ baseURL: API_URL });
  const res = await login.post("/api/v2/auth/token", {
    data: { username: process.env.OPENCORTYX_ADMIN_EMAIL, password: process.env.OPENCORTYX_ADMIN_PASSWORD },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  const { access_token } = await res.json();
  await login.dispose();
  return request.newContext({ baseURL: API_URL, extraHTTPHeaders: { Authorization: `Bearer ${access_token}` } });
}

/** Uploads a document, waits until its ingestion run completes, and returns the run and asset ids. */
export async function ingestDocument(api: APIRequestContext, name: string, text: string, timeoutMs = 600_000) {
  const upload = await api.post("/api/v2/sources/upload", {
    multipart: { file: { name, mimeType: "text/markdown", buffer: Buffer.from(text) } },
  });
  expect(upload.ok(), await upload.text()).toBeTruthy();
  const { run_id } = await upload.json();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const run = await (await api.get(`/api/v2/progress/${run_id}`)).json();
    if (["completed", "failed", "canceled"].includes(run.status)) {
      expect(run.status, run.error ?? "").toBe("completed");
      return { runId: run_id as string, assetId: run.source_ref as string };
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
  throw new Error(`ingestion run ${run_id} did not finish`);
}

/** Permanently deletes an asset created by a test, with everything derived from it. */
export async function deleteAsset(api: APIRequestContext, assetId: string) {
  const res = await api.delete(`/api/v2/assets/${assetId}`);
  expect(res.ok(), await res.text()).toBeTruthy();
}
