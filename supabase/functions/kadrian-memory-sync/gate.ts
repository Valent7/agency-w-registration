import {
  assert,
  canonical,
  emptyDocument,
  firstRow,
  hash,
  Meta,
  Scope,
} from "./protocol.ts";
import type { Store } from "./service.ts";

/** Per-request generation. No direct table reads or calls to the old commit RPC.
 * SQL gates EACH operation under the same lock used by the operator stop.
 */
export class GatedMemoryStore implements Store {
  constructor(
    private url: string,
    private key: string,
    private pilot: Scope,
    private release: string,
    private fetcher: typeof fetch = fetch,
    private device = "",
    private generation: number | null = null,
  ) {
    assert(/^https:\/\/[a-z0-9]+\.supabase\.co$/.test(url));
    assert(key.length > 20 && /^[a-f0-9]{32}$/.test(release));
  }
  private async call(
    action: string,
    scope: Scope,
    extra: Record<string, unknown> = {},
    document?: string,
  ) {
    const raw = canonical({
      p_action: action,
      p_release: this.release,
      p_device: this.device,
      p_owner: this.pilot.owner_id,
      p_agent: this.pilot.agent_id,
      p_workspace: this.pilot.workspace_id,
      p_project: this.pilot.project_id,
      p_generation: this.generation,
      p_scope: scope,
      ...extra,
    });
    const body = document === undefined
      ? raw
      : raw.slice(0, -1) + ',"p_document":' + document + "}";
    const response = await this.fetcher(
      this.url + "/rest/v1/rpc/kadrian_memory_exchange",
      {
        method: "POST",
        headers: {
          apikey: this.key,
          Authorization: "Bearer " + this.key,
          "Content-Type": "application/json",
        },
        body,
        redirect: "error",
        signal: AbortSignal.timeout(8000),
      },
    );
    assert(response.ok, "Gate closed");
    const text = await response.text();
    assert(new TextEncoder().encode(text).length < 3_000_000);
    return text;
  }
  async authorize(scope: Scope, device: string): Promise<Store> {
    assert(canonical(scope) === canonical(this.pilot));
    const session = new GatedMemoryStore(
      this.url,
      this.key,
      this.pilot,
      this.release,
      this.fetcher,
      device,
    );
    const value = JSON.parse(await session.call("begin", scope));
    assert(Number.isSafeInteger(value.generation) && value.generation > 0);
    session.generation = value.generation;
    return session;
  }
  async metadata(scope: Scope): Promise<Meta> {
    assert(this.generation !== null);
    const rows = JSON.parse(await this.call("metadata", scope));
    return rows[0] ?? { revision: 0, hash: await hash(emptyDocument) };
  }
  async read(scope: Scope) {
    assert(this.generation !== null);
    return firstRow(await this.call("read", scope)) ??
      '{"revision":0,"document":' + emptyDocument + ',"hash":"' +
        await hash(emptyDocument) + '"}';
  }
  async commit(
    scope: Scope,
    expected: number,
    document: string,
    event: unknown,
    key: string,
    docHash: string,
  ) {
    assert(this.generation !== null);
    const row = firstRow(
      await this.call("commit", scope, {
        p_expected: expected,
        p_event: event,
        p_key: key,
        p_hash: docHash,
      }, document),
    );
    assert(row);
    return row;
  }
}
