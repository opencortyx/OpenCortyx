import { describe, expect, it } from "@jest/globals";
import { retryDelay, uploadAll } from "./uploadQueue";

const noSleep = async () => {};
const httpError = (status: number, headers: Record<string, string> = {}, detail?: string) =>
  ({ response: { status, headers, data: detail ? { detail } : {} } });

describe("uploadAll", () => {
  it("never has more than `concurrency` uploads in flight", async () => {
    let inFlight = 0, peak = 0;
    await uploadAll(20, async () => {
      inFlight++; peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
    }, { concurrency: 3, sleep: noSleep });
    expect(peak).toBe(3);
  });

  it("retries a rate-limited file until it goes through", async () => {
    let calls = 0;
    const out = await uploadAll(1, async () => {
      if (++calls < 3) throw httpError(429, { "retry-after": "1" });
      return "run-1";
    }, { sleep: noSleep });
    expect(out[0]).toEqual({ index: 0, ok: true, value: "run-1" });
    expect(calls).toBe(3);
  });

  it("does not retry a file the server refused, and says why", async () => {
    let calls = 0;
    const out = await uploadAll(1, async () => {
      calls++;
      throw httpError(415, {}, "'x.png' is a PNG image file, which OpenCortyx cannot read.");
    }, { sleep: noSleep });
    expect(calls).toBe(1);
    expect(out[0]).toEqual({ index: 0, ok: false, error: "'x.png' is a PNG image file, which OpenCortyx cannot read." });
  });

  it("gives up after the last attempt and keeps the other files going", async () => {
    const out = await uploadAll(3, async (i) => {
      if (i === 1) throw new Error("socket hang up");
      return i;
    }, { attempts: 2, sleep: noSleep });
    expect(out.map((r) => r.ok)).toEqual([true, false, true]);
    expect(out[1]).toMatchObject({ ok: false, error: "network error: socket hang up" });
  });
});

describe("retryDelay", () => {
  it("honours Retry-After, else backs off exponentially", () => {
    expect(retryDelay(httpError(429, { "retry-after": "7" }), 0)).toBe(7000);
    expect(retryDelay(httpError(503), 0, 1000)).toBe(1000);
    expect(retryDelay(httpError(503), 3, 1000)).toBe(8000);
    expect(retryDelay(httpError(503), 10, 1000)).toBe(30000);
    expect(retryDelay(httpError(413), 0)).toBeNull();
    expect(retryDelay(new Error("offline"), 0, 500)).toBe(500);
  });
});
