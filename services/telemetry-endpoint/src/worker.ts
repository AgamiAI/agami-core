/**
 * agami telemetry endpoint — Cloudflare Worker.
 *
 * Receives anonymous usage events from the agami skill running on users'
 * machines. Hard-coded allowlist matches plugins/agami/shared/telemetry-payload.md.
 * Defense in depth: even if the open-source skill is tampered with to send
 * extra fields, this endpoint rejects them.
 *
 * Storage: appends accepted events to a daily JSONL log in R2 (one object per
 * UTC day). Aggregation happens out-of-band (a separate scheduled job reads
 * the JSONL files into DuckDB and applies outlier-aware aggregation per the
 * deployment README).
 *
 * Rate limit: 100 req/min per IP, enforced by the bound rate-limiter binding
 * (configured in wrangler.toml). Cloudflare bot fight mode is enabled at the
 * zone level — also configured in wrangler.toml.
 */

export interface Env {
  // R2 bucket for daily JSONL logs
  TELEMETRY_BUCKET: R2Bucket;
  // Rate limit binding (configured in wrangler.toml)
  RATE_LIMITER: RateLimit;
}

const ALLOWED_FIELDS = new Set<string>([
  "schema_version",
  "event_type",
  "install_id",
  "db_type",
  "os",
  "host",
  "error_kind",
  "latency_p50_ms",
  "latency_p95_ms",
  "tier",
  "client_version",
  "timestamp",
]);

const ALLOWED_EVENT_TYPES = new Set<string>([
  "install", "connect", "query", "correction", "chart", "error", "update_check",
]);

const ALLOWED_DB_TYPES = new Set<string>(["postgres", "mysql", "sqlite"]);

const ALLOWED_OS = new Set<string>(["darwin", "linux", "windows"]);

const ALLOWED_HOSTS = new Set<string>([
  "claude-code-cli", "claude-code-vscode", "claude-code-cursor", "claude-cowork",
]);

const ALLOWED_ERROR_KINDS = new Set<string>([
  "auth", "dsn", "network", "permission", "column_not_found",
  "table_not_found", "syntax", "timeout", "driver_missing", "other",
]);

const ALLOWED_TIERS = new Set<string>(["cli", "duckdb", "python"]);

const REQUIRED_FIELDS = [
  "event_type", "install_id", "db_type", "os", "host", "tier",
  "client_version", "timestamp",
];

const SUPPORTED_SCHEMA_VERSION = 1;
const MAX_BATCH = 100;
const MAX_BODY_BYTES = 64 * 1024; // 64KB; a 100-event batch is ~25KB
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

interface Event {
  event_type: string;
  install_id: string;
  db_type: string;
  os: string;
  host: string;
  tier: string;
  client_version: string;
  timestamp: string;
  error_kind?: string;
  latency_p50_ms?: number;
  latency_p95_ms?: number;
}

interface Payload {
  schema_version: number;
  events: Event[];
}

function reject(status: number, message: string): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function isValidEvent(e: unknown): e is Event {
  if (!e || typeof e !== "object" || Array.isArray(e)) return false;
  const obj = e as Record<string, unknown>;

  // No extras
  for (const key of Object.keys(obj)) {
    if (!ALLOWED_FIELDS.has(key)) return false;
  }
  // All required present
  for (const req of REQUIRED_FIELDS) {
    if (obj[req] === undefined || obj[req] === null) return false;
  }
  // Type checks
  if (typeof obj.event_type !== "string" || !ALLOWED_EVENT_TYPES.has(obj.event_type)) return false;
  if (typeof obj.install_id !== "string" || !UUID_RE.test(obj.install_id)) return false;
  if (typeof obj.db_type !== "string" || !ALLOWED_DB_TYPES.has(obj.db_type)) return false;
  if (typeof obj.os !== "string" || !ALLOWED_OS.has(obj.os)) return false;
  if (typeof obj.host !== "string" || !ALLOWED_HOSTS.has(obj.host)) return false;
  if (typeof obj.tier !== "string" || !ALLOWED_TIERS.has(obj.tier)) return false;
  if (typeof obj.client_version !== "string" || obj.client_version.length > 32) return false;
  if (typeof obj.timestamp !== "string" || !ISO_RE.test(obj.timestamp)) return false;
  if (obj.error_kind !== undefined) {
    if (typeof obj.error_kind !== "string" || !ALLOWED_ERROR_KINDS.has(obj.error_kind)) return false;
  }
  for (const k of ["latency_p50_ms", "latency_p95_ms"] as const) {
    const v = obj[k];
    if (v !== undefined) {
      if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 600_000) return false;
    }
  }
  return true;
}

async function appendToR2(env: Env, events: Event[]): Promise<void> {
  // One object per UTC day. We append by reading-modify-write; for v1 traffic
  // (open-source release, expected DAU < 10k) this is fine. If write contention
  // becomes a problem, switch to per-shard objects keyed by install_id prefix.
  const day = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
  const key = `events/${day}.jsonl`;

  const existing = await env.TELEMETRY_BUCKET.get(key);
  const existingText = existing ? await existing.text() : "";

  const newLines = events.map((e) => JSON.stringify(e)).join("\n") + "\n";
  await env.TELEMETRY_BUCKET.put(key, existingText + newLines, {
    httpMetadata: { contentType: "application/x-ndjson" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "POST") {
      return reject(405, "method not allowed");
    }
    if (new URL(request.url).pathname !== "/v1/events") {
      return reject(404, "not found");
    }

    // Rate limit (Cloudflare-managed, see wrangler.toml)
    const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const { success } = await env.RATE_LIMITER.limit({ key: ip });
    if (!success) {
      return reject(429, "rate limit exceeded");
    }

    // Body size cap
    const lengthHeader = request.headers.get("content-length");
    if (lengthHeader && parseInt(lengthHeader) > MAX_BODY_BYTES) {
      return reject(413, "payload too large");
    }

    let parsed: unknown;
    try {
      const text = await request.text();
      if (text.length > MAX_BODY_BYTES) return reject(413, "payload too large");
      parsed = JSON.parse(text);
    } catch {
      return reject(400, "invalid JSON");
    }

    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return reject(400, "payload must be an object");
    }
    const payload = parsed as Partial<Payload>;
    if (payload.schema_version !== SUPPORTED_SCHEMA_VERSION) {
      return reject(400, `unsupported schema_version (expected ${SUPPORTED_SCHEMA_VERSION})`);
    }
    if (!Array.isArray(payload.events) || payload.events.length === 0) {
      return reject(400, "events must be a non-empty array");
    }
    if (payload.events.length > MAX_BATCH) {
      return reject(400, `batch too large (max ${MAX_BATCH})`);
    }

    const validated: Event[] = [];
    for (const ev of payload.events) {
      if (!isValidEvent(ev)) {
        return reject(400, "event failed validation");
      }
      validated.push(ev);
    }

    try {
      await appendToR2(env, validated);
    } catch (err) {
      // Don't expose internal errors to the public client.
      console.error("R2 append failed:", err);
      return reject(500, "storage unavailable");
    }

    return new Response(JSON.stringify({ accepted: validated.length }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  },
};
