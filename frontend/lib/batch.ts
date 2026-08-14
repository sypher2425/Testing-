export const MAX_BATCH_ITEMS = 20;

const UNIVERSAL_TRACKING_PARAMS = new Set([
  "fbclid",
  "gclid",
  "utm_campaign",
  "utm_content",
  "utm_medium",
  "utm_source",
  "utm_term",
]);

export interface ParsedBulkUrls {
  urls: string[];
  invalid: string[];
  duplicateCount: number;
  overflowCount: number;
}

function cleanToken(token: string): string {
  return token
    .replace(/^(?:[-*•]|\d+[.)])\s*/, "")
    .replace(/^[<(\[]+/, "")
    .replace(/[>),.;\]]+$/, "")
    .trim();
}

/** Parse pasted links defensively: accepts one-per-line, whitespace-separated
 * links, and common bulleted/numbered lists. Tracking parameters and hashes
 * are removed before deduplication so the same video is not queued twice. */
export function parseBulkUrls(input: string, limit = MAX_BATCH_ITEMS): ParsedBulkUrls {
  const tokens = input
    .split(/\r?\n/)
    .flatMap((line) => line.trim().split(/\s+/))
    .map(cleanToken)
    .filter(Boolean);

  const urls: string[] = [];
  const invalid: string[] = [];
  const seen = new Set<string>();
  let duplicateCount = 0;
  let overflowCount = 0;

  for (const token of tokens) {
    let parsed: URL;
    try {
      parsed = new URL(token);
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) throw new Error();
      if (parsed.username || parsed.password) throw new Error();
    } catch {
      invalid.push(token);
      continue;
    }

    parsed.hash = "";
    const hostname = parsed.hostname.toLocaleLowerCase();
    for (const key of Array.from(parsed.searchParams.keys())) {
      const lowerKey = key.toLocaleLowerCase();
      const platformTracking =
        (lowerKey === "si" && (hostname === "youtu.be" || hostname.endsWith(".youtube.com"))) ||
        (lowerKey === "igshid" && (hostname === "instagram.com" || hostname.endsWith(".instagram.com")));
      if (UNIVERSAL_TRACKING_PARAMS.has(lowerKey) || platformTracking) {
        parsed.searchParams.delete(key);
      }
    }
    parsed.searchParams.sort();
    const normalized = parsed.toString();
    const key = normalized;
    if (seen.has(key)) {
      duplicateCount += 1;
      continue;
    }
    seen.add(key);
    if (urls.length >= limit) {
      overflowCount += 1;
      continue;
    }
    urls.push(normalized);
  }

  return { urls, invalid, duplicateCount, overflowCount };
}

export type BatchRunResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: unknown };

/** Run independent submissions with a strict upper bound. URL creation can
 * use a small burst; large file streams should pass concurrency=1 to avoid
 * disk-headroom races and competing uploads. */
export async function runWithConcurrency<T, R>(
  items: T[],
  concurrency: number,
  worker: (item: T, index: number) => Promise<R>
): Promise<BatchRunResult<R>[]> {
  const results = new Array<BatchRunResult<R>>(items.length);
  let cursor = 0;

  async function runNext() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      try {
        results[index] = { ok: true, value: await worker(items[index]!, index) };
      } catch (error) {
        results[index] = { ok: false, error };
      }
    }
  }

  const workerCount = Math.max(1, Math.min(Math.floor(concurrency), items.length));
  await Promise.all(Array.from({ length: workerCount }, runNext));
  return results;
}
