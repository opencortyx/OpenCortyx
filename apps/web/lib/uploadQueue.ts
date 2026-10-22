/**
 * Send many files: a few at a time, retrying what failed for a passing reason.
 *
 * Add data sent a dropped folder one file after another and counted any error as a failed file. A
 * busy moment (HTTP 429, a restarting API, a dropped connection) therefore lost files for good, and
 * the person saw "Added 412 of 1000" with no way to tell which or why. This is the usual bulk-upload
 * shape (a bounded queue, as Uppy's `limit`): `concurrency` requests in flight, a retry with
 * exponential backoff for errors that pass, honouring the server's Retry-After, and a final error per
 * file that says what the server said.
 */

export type UploadResult<T> = { index: number; ok: true; value: T } | { index: number; ok: false; error: string };

export type UploadOptions = {
  concurrency?: number;
  /** attempts per file, the first included */
  attempts?: number;
  /** first backoff in ms; doubles each retry, capped at 30 s */
  baseDelayMs?: number;
  onSettled?: (r: UploadResult<unknown>, done: number) => void;
  sleep?: (ms: number) => Promise<void>;
};

/** Statuses worth another try: rate limited, or the server/proxy briefly unavailable. */
const RETRY_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

/** Whether an error from axios (or fetch-like code) is worth retrying, and after how long. */
export function retryDelay(error: any, attempt: number, baseDelayMs = 1000): number | null {
  const status: number | undefined = error?.response?.status;
  if (status !== undefined && !RETRY_STATUS.has(status)) return null;   // a 4xx about the file itself
  const header = error?.response?.headers?.["retry-after"];
  const asked = header !== undefined ? Number(header) * 1000 : NaN;
  if (Number.isFinite(asked) && asked >= 0) return Math.min(asked, 60_000);
  return Math.min(baseDelayMs * 2 ** attempt, 30_000);
}

export function describeError(error: any): string {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (error?.response?.status) return `HTTP ${error.response.status}`;
  return error?.message ? `network error: ${error.message}` : "upload failed";
}

export async function uploadAll<T>(
  count: number,
  send: (index: number) => Promise<T>,
  opts: UploadOptions = {},
): Promise<UploadResult<T>[]> {
  const concurrency = Math.max(1, opts.concurrency ?? 3);
  const attempts = Math.max(1, opts.attempts ?? 6);
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const results: UploadResult<T>[] = new Array(count);
  let next = 0;
  let done = 0;

  const one = async (index: number): Promise<UploadResult<T>> => {
    for (let attempt = 0; ; attempt++) {
      try {
        return { index, ok: true, value: await send(index) };
      } catch (e) {
        const wait = attempt + 1 < attempts ? retryDelay(e, attempt, opts.baseDelayMs) : null;
        if (wait === null) return { index, ok: false, error: describeError(e) };
        await sleep(wait + Math.floor(Math.random() * 250));   // jitter: retries do not arrive together
      }
    }
  };

  const worker = async () => {
    while (next < count) {
      const index = next++;
      const r = await one(index);
      results[index] = r;
      done++;
      opts.onSettled?.(r as UploadResult<unknown>, done);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, count) }, worker));
  return results;
}
