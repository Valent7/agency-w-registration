// No external dependencies. Existing PR85 schema/RPC; no migrations or agent imports.
export type Scope = {
  owner_id: number;
  agent_id: string;
  workspace_id: string;
  project_id: string | null;
};
export type Meta = { revision: number; hash: string };
export type Device = { scope: Scope; public_key: string; revoked?: boolean };
export type Registry = Record<string, Device>;
export const MAX_REQUEST = 3_000_000;
export const MAX_DOCUMENT = 1_600_000; // headroom below existing 2MB JSONB textual limit
const encoder = new TextEncoder();
export const bytes = (s: string) => encoder.encode(s);
function assert(ok: unknown, message = "Invalid request"): asserts ok {
  if (!ok) throw new Error(message);
}
function object(v: unknown): v is Record<string, any> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
function exact(v: unknown, keys: string[]) {
  assert(object(v) && Object.keys(v).sort().join() === keys.sort().join());
}
function integer(v: unknown, min = 0): v is number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= min;
}
// Python sorts Unicode code points, not JavaScript UTF-16 units.
function compare(a: string, b: string) {
  const aa = Array.from(a, (x) => x.codePointAt(0)!);
  const bb = Array.from(b, (x) => x.codePointAt(0)!);
  for (let i = 0; i < Math.min(aa.length, bb.length); i++) {
    if (aa[i] !== bb[i]) return aa[i] - bb[i];
  }
  return aa.length - bb.length;
}
export function canonical(v: any): string {
  if (v === null || typeof v === "boolean") return JSON.stringify(v);
  if (typeof v === "string") {
    assert(
      !/[\uD800-\uDFFF]/u.test(
        v.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ""),
      ),
      "Invalid Unicode",
    );
    return JSON.stringify(v);
  }
  if (typeof v === "number") {
    assert(Number.isSafeInteger(v), "Only safe integers in signed protocol");
    return String(v);
  }
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  assert(object(v));
  return "{" +
    Object.keys(v).sort(compare).map((k) =>
      canonical(k) + ":" + canonical(v[k])
    ).join(",") + "}";
}
export async function hash(raw: string) {
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes(raw))),
    (x) => x.toString(16).padStart(2, "0"),
  ).join("");
}
export const digest = (v: any) => hash(canonical(v));
export function validScope(s: Scope, project = true) {
  exact(s, ["owner_id", "agent_id", "workspace_id", "project_id"]);
  assert(integer(s.owner_id, 1));
  for (const value of [s.agent_id, s.workspace_id]) {
    assert(typeof value === "string" && /^[a-zA-Z0-9_*.-]{1,100}$/.test(value));
  }
  assert(
    project
      ? typeof s.project_id === "string" &&
        /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(s.project_id) &&
        s.agent_id !== "*"
      : s.project_id === null && s.workspace_id === "owner",
  );
}
function unbase64(s: string) {
  assert(typeof s === "string" && /^[A-Za-z0-9+/]*={0,2}$/.test(s));
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}
export async function authenticate(
  envelope: any,
  registry: Registry,
  pilot: Scope,
  now: number,
) {
  exact(envelope, ["request", "signature"]);
  const r = envelope.request;
  exact(r, [
    "format",
    "device_id",
    "scope",
    "operation",
    "payload",
    "issued_at",
    "request_id",
  ]);
  assert(r.format === "agency-edge-sync/1" && r.operation === "sync");
  assert(
    typeof r.device_id === "string" &&
      /^[a-zA-Z0-9_-]{1,100}$/.test(r.device_id),
  );
  const d = Object.hasOwn(registry, r.device_id) ? registry[r.device_id] : null;
  assert(d && !d.revoked, "Device unavailable");
  validScope(d.scope);
  validScope(pilot);
  assert(
    canonical(d.scope) === canonical(pilot) &&
      canonical(r.scope) === canonical(d.scope),
    "Scope unavailable",
  );
  assert(
    integer(r.issued_at) && Math.abs(now - r.issued_at) <= 300,
    "Expired request",
  );
  assert(
    typeof r.request_id === "string" && /^[a-f0-9]{32}$/.test(r.request_id),
  );
  const key = await crypto.subtle.importKey(
    "raw",
    unbase64(d.public_key),
    { name: "Ed25519" },
    false,
    ["verify"],
  );
  assert(
    await crypto.subtle.verify(
      "Ed25519",
      key,
      unbase64(envelope.signature),
      bytes(canonical(r)),
    ),
    "Invalid signature",
  );
  return { request: r, scope: d.scope };
}
export function safeText(value: any, limit = 512_000) {
  const raw = canonical(value);
  assert(bytes(raw).length <= limit, "Content too large");
  assert(
    !/(bearer\s+[a-z0-9._-]{12,}|sk-(?:proj-)?[a-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY|eyJ[a-z0-9_-]{15,}\.[a-z0-9_-]+\.[a-z0-9_-]+|(?:api[_-]?key|access[_-]?token|secret|password)\s*["\s]*[:=]\s*["\s]*[^\s"}]{6,})/i
      .test(raw),
    "Credentials prohibited",
  );
}
export async function validateEvent(e: any, device: string) {
  exact(e, ["sequence", "base_revision", "files", "file_modes", "event_id"]);
  assert(integer(e.sequence, 1) && integer(e.base_revision));
  const files = [
    "context/PROJECT.md",
    "context/DECISIONS.md",
    "context/FEEDBACK.md",
  ];
  assert(
    object(e.files) && Object.keys(e.files).length > 0 &&
      Object.keys(e.files).every((k) =>
        files.includes(k) && typeof e.files[k] === "string"
      ),
  );
  exact(e.file_modes, Object.keys(e.files));
  for (const [name, info] of Object.entries(e.file_modes) as [string, any][]) {
    exact(info, ["mode", "base_hash", "result_hash"]);
    assert(
      ["snapshot", "append"].includes(info.mode) &&
        /^[a-f0-9]{64}$/.test(info.result_hash),
    );
    assert(
      info.base_hash === null ||
        (typeof info.base_hash === "string" &&
          /^[a-f0-9]{64}$/.test(info.base_hash)),
    );
    if (info.mode === "snapshot") {
      assert(
        await digest(e.files[name]) === info.result_hash,
        "File checksum mismatch",
      );
    } else {assert(
        typeof info.base_hash === "string",
        "Append checkpoint required",
      );}
  }
  const { event_id, ...rest } = e;
  assert(
    event_id === await digest({ device, ...rest }),
    "Event checksum mismatch",
  );
  safeText(e);
}
// Raw JSON spans preserve existing CPython numeric lexemes (1.0, 1e-07, bigint).
// JSON.parse is used only to inspect shapes, NEVER to reserialize an old document.
export function valueEnd(s: string, start: number): number {
  let i = start;
  while (/\s/.test(s[i] ?? "") && i < s.length) i++;
  if (s[i] === '"') {
    for (i++; i < s.length; i++) {
      if (s[i] === "\\") i++;
      else if (s[i] === '"') return i + 1;
    }
    throw new Error("Unclosed JSON string");
  }
  if (s[i] === "{" || s[i] === "[") {
    const close = s[i] === "{" ? "}" : "]";
    for (i++; i < s.length;) {
      if (s[i] === close) return i + 1;
      if (/[\s,:]/.test(s[i])) i++;
      else i = valueEnd(s, i);
    }
    throw new Error("Unclosed JSON container");
  }
  while (i < s.length && !/[\s,}\]]/.test(s[i])) i++;
  assert(i > start, "Invalid JSON span");
  return i;
}
function members(
  s: string,
  start = 0,
): { key: string; start: number; value: number; end: number }[] {
  assert(s[start] === "{");
  const out: { key: string; start: number; value: number; end: number }[] = [];
  const seen = new Set<string>();
  let i = start + 1;
  while (i < s.length) {
    while (/[\s,]/.test(s[i] ?? "") && i < s.length) i++;
    if (s[i] === "}") return out;
    const begin = i;
    const end = valueEnd(s, i);
    const key = JSON.parse(s.slice(i, end));
    assert(typeof key === "string" && !seen.has(key));
    i = end;
    while (/\s/.test(s[i] ?? "") && i < s.length) i++;
    assert(s[i++] === ":");
    while (/\s/.test(s[i] ?? "") && i < s.length) i++;
    seen.add(key);
    const value = i;
    i = valueEnd(s, i);
    out.push({ key, start: begin, value, end: i });
  }
  throw new Error("Invalid object");
}
export function appendEntry(raw: string, id: string, entry: any): string {
  assert(bytes(raw).length <= MAX_DOCUMENT, "Document capacity reached");
  const parsed = JSON.parse(raw);
  assert(parsed.schema === 1 && object(parsed.entries) && object(parsed.tasks));
  assert(!Object.hasOwn(parsed.entries, id), "Entry already exists");
  const prop = members(raw).find((x) => x.key === "entries");
  assert(prop);
  const items = members(raw, prop.value);
  const next = items.find((x) => compare(x.key, id) > 0);
  const addition = canonical(id) + ":" + canonical(entry);
  const result = next
    ? raw.slice(0, next.start) + addition + "," + raw.slice(next.start)
    : raw.slice(0, prop.end - 1) + (items.length ? "," : "") + addition +
      raw.slice(prop.end - 1);
  assert(bytes(result).length <= MAX_DOCUMENT, "Document capacity reached");
  return result;
}
export function firstRow(raw: string): string | null {
  JSON.parse(raw);
  let i = raw.indexOf("[") + 1;
  assert(i > 0);
  while (/\s/.test(raw[i] ?? "") && i < raw.length) i++;
  if (raw[i] === "]") return null;
  const end = valueEnd(raw, i);
  assert(raw.slice(end).trim() === "]", "Expected one row");
  return raw.slice(i, end);
}
export function propertyRaw(raw: string, key: string) {
  const m = members(raw).find((x) => x.key === key);
  assert(m);
  return raw.slice(m.value, m.end);
}
export const emptyDocument = canonical({ schema: 1, entries: {}, tasks: {} });
export { assert, exact, integer, object };
