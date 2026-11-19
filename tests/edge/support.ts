import {
  canonical,
  emptyDocument,
  hash,
  Meta,
  propertyRaw,
  Scope,
} from "../../supabase/functions/kadrian-memory-sync/protocol.ts";
import { Store } from "../../supabase/functions/kadrian-memory-sync/service.ts";
export function eq(a: unknown, b: unknown) {
  if (canonical(a) !== canonical(b)) {
    throw new Error(
      "Assertion failed: " + JSON.stringify(a) + " != " + JSON.stringify(b),
    );
  }
}
export function ok(v: unknown) {
  if (!v) throw new Error("Assertion failed");
}
export async function rejects(fn: () => unknown) {
  let raised = false;
  try {
    await fn();
  } catch {
    raised = true;
  }
  ok(raised);
}
export const pilot: Scope = {
  owner_id: 42,
  agent_id: "kadrian",
  workspace_id: "11111111-1111-4111-8111-111111111111",
  project_id: "11111111-1111-4111-8111-111111111111",
};
export class FakeStore implements Store {
  rows = new Map<string, string>();
  reads = 0;
  metas = 0;
  writes = 0;
  fail = false;
  lost = false;
  key(s: Scope) {
    return canonical(s);
  }
  async metadata(s: Scope): Promise<Meta> {
    this.metas++;
    const r = JSON.parse(await this.peek(s));
    return { revision: r.revision, hash: r.hash };
  }
  async peek(s: Scope) {
    return this.rows.get(this.key(s)) ??
      '{"revision":0,"document":' + emptyDocument + ',"hash":"' +
        await hash(emptyDocument) + '"}';
  }
  async read(s: Scope) {
    this.reads++;
    return await this.peek(s);
  }
  async seed(s: Scope, document: string, revision = 1) {
    this.rows.set(
      this.key(s),
      '{"revision":' + revision + ',"document":' + document + ',"hash":"' +
        await hash(document) +
        '","event":{},"idempotency_key":"seed","created_at":"2026-11-19T00:00:00Z"}',
    );
  }
  async commit(
    s: Scope,
    expected: number,
    document: string,
    event: any,
    key: string,
    docHash: string,
  ) {
    if (this.fail) throw new Error("CAS busy");
    const row = JSON.parse(await this.peek(s));
    eq(row.revision, expected);
    eq(await hash(document), docHash);
    this.writes++;
    const raw = '{"revision":' + (expected + 1) + ',"document":' + document +
      ',"hash":"' + docHash + '","event":' + canonical(event) +
      ',"idempotency_key":' + canonical(key) +
      ',"created_at":"2026-11-19T00:00:00Z"}';
    this.rows.set(this.key(s), raw);
    if (this.lost) {
      this.lost = false;
      throw new Error("Lost response after commit");
    }
    return raw;
  }
}
export async function keys() {
  const k = await crypto.subtle.generateKey("Ed25519", true, [
    "sign",
    "verify",
  ]) as CryptoKeyPair;
  const pub = btoa(
    String.fromCharCode(
      ...new Uint8Array(await crypto.subtle.exportKey("raw", k.publicKey)),
    ),
  );
  return { k, pub };
}
export async function envelope(k: CryptoKey, payload: any, changes: any = {}) {
  const r = {
    format: "agency-edge-sync/1",
    device_id: "synthetic",
    scope: pilot,
    operation: "sync",
    payload,
    issued_at: 1000,
    request_id: "a".repeat(32),
    ...changes,
  };
  return {
    request: r,
    signature: btoa(
      String.fromCharCode(
        ...new Uint8Array(
          await crypto.subtle.sign(
            "Ed25519",
            k,
            new TextEncoder().encode(canonical(r)),
          ),
        ),
      ),
    ),
  };
}
export function request(e: any) {
  return new Request(
    "https://synthetic.supabase.co/functions/v1/kadrian-memory-sync",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: canonical(e),
    },
  );
}
export { propertyRaw };
