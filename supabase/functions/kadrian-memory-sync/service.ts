import {
  appendEntry,
  assert,
  authenticate,
  bytes,
  canonical,
  digest,
  emptyDocument,
  exact,
  firstRow,
  hash,
  integer,
  MAX_REQUEST,
  Meta,
  object,
  propertyRaw,
  Registry,
  Scope,
  validateEvent,
} from "./protocol.ts";

export interface Store {
  metadata(scope: Scope): Promise<Meta>;
  read(scope: Scope): Promise<string>; // one raw JSON row; never reserialize old document
  commit(
    scope: Scope,
    expected: number,
    document: string,
    event: any,
    key: string,
    hash: string,
  ): Promise<string>;
}
export type Quota = {
  observed_at: number;
  period_end: number;
  invocations: number;
  included_invocations: number;
  egress_bytes: number;
  included_egress_bytes: number;
};
export type Config = {
  enabled: boolean;
  pilot: Scope;
  devices: Registry;
  quota?: Quota;
};
function parent(scope: Scope, agent: string): Scope {
  return { ...scope, agent_id: agent, workspace_id: "owner", project_id: null };
}
function quotaStatus(q: Quota | undefined, now: number) {
  if (
    !q || !Object.values(q).every((x) => Number.isFinite(x) && x >= 0) ||
    q.observed_at > now || now - q.observed_at > 86400 || q.period_end <= now ||
    q.included_invocations <= 0 || q.included_egress_bytes <= 0
  ) {
    return {
      source: "operator_observation",
      status: "unknown_or_stale",
      warning:
        "Общая квота Supabase не подтверждена свежими данными. Локальный счётчик не заменяет Billing / Usage.",
    };
  }
  const ratio = Math.max(
    q.invocations / q.included_invocations,
    q.egress_bytes / q.included_egress_bytes,
  );
  return {
    source: "operator_observation",
    status: ratio >= 0.95
      ? "critical"
      : ratio >= 0.8
      ? "warning"
      : "within_observed_quota",
    observed_at: q.observed_at,
    period_end: q.period_end,
    warning: ratio >= 0.8
      ? "Общая квота Supabase приближается к пределу. Проверьте Billing / Usage; очередь сохранится при остановке."
      : null,
  };
}
async function limitedBody(req: Request) {
  assert(
    Number(req.headers.get("content-length") ?? 0) <= MAX_REQUEST,
    "Request too large",
  );
  assert(req.body);
  const reader = req.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      assert(size <= MAX_REQUEST, "Request too large");
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    all.set(c, at);
    at += c.length;
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(all));
}
export function handler(
  config: Config,
  store: Store,
  clock = () => Date.now() / 1000,
) {
  return async (req: Request): Promise<Response> => {
    const headers = {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    };
    try {
      assert(config.enabled, "Pilot disabled");
      assert(
        req.method === "POST" &&
          new URL(req.url).pathname.endsWith("/kadrian-memory-sync") &&
          req.headers.get("content-type")?.split(";")[0] === "application/json",
      );
      const { request: r, scope } = await authenticate(
        await limitedBody(req),
        config.devices,
        config.pilot,
        clock(),
      );
      // Authentication and exact pilot binding precede EVERY database access.
      const p = r.payload;
      exact(p, ["known", "event", "base_document"]);
      assert(p.known === null || object(p.known));
      if (p.known !== null) {
        exact(p.known, ["project", "global", "agent"]);
        for (const m of Object.values(p.known) as any[]) {
          exact(m, ["revision", "hash"]);
          assert(integer(m.revision) && /^[a-f0-9]{64}$/.test(m.hash));
        }
      }
      assert(p.event === null || object(p.event));
      if (p.event !== null) await validateEvent(p.event, r.device_id);
      assert(
        p.event !== null
          ? (p.base_document === null || typeof p.base_document === "string")
          : p.base_document === null,
      );
      const scopes = [scope, parent(scope, "*"), parent(scope, scope.agent_id)];
      const names = ["project", "global", "agent"];
      const mm = await Promise.all(scopes.map((s) => store.metadata(s)));
      let meta = Object.fromEntries(names.map((n, i) => [n, mm[i]]));
      let row: string | null = null;
      let ack: any = null;
      let needBase = false;
      if (p.event !== null) {
        const e = p.event;
        assert(
          e.base_revision <= meta.project.revision,
          "Client ahead of cloud",
        );
        const key = "device-event:" + r.device_id + ":" + e.sequence;
        const id = (await digest({ scope, key })).slice(0, 32);
        // A lost response can be replayed. Inspect latest immutable document only for
        // actual pending writes; idle polls stay metadata-only.
        row = await store.read(scope);
        const current = JSON.parse(row);
        assert(current.revision >= meta.project.revision, "History rewind");
        meta.project = { revision: current.revision, hash: current.hash };
        const prior = current.document.entries[id];
        if (prior) {
          assert(
            prior.evidence?.event_id === e.event_id,
            "Sequence reused with different content",
          );
          ack = {
            event_id: e.event_id,
            revision: current.revision,
            conflict: prior.evidence.conflict,
          };
        } else if (
          p.base_document === null ||
          await hash(p.base_document) !== current.hash
        ) needBase = true;
        else {
          const conflict = e.base_revision !== current.revision;
          const entry = {
            id,
            kind: "legacy",
            text: canonical({ files: e.files, file_modes: e.file_modes }),
            source: "device-context-snapshot",
            certainty: "unknown",
            status: "recorded",
            related: [],
            evidence: {
              event_id: e.event_id,
              device_id: r.device_id,
              base_revision: e.base_revision,
              cloud_revision: current.revision,
              conflict,
              file_modes: e.file_modes,
            },
          };
          const document = appendEntry(p.base_document, id, entry);
          const entryEvent = {
            kind: "legacy",
            entry_id: id,
            actor: "device:" + r.device_id,
            actor_kind: "sync",
            at: new Date(clock() * 1000).toISOString(),
          };
          // RPC performs existing owner/project/CAS/idempotency checks in one transaction.
          // If response is lost/CAS fails, do not acknowledge. Durable retry reconciles.
          row = await store.commit(
            scope,
            current.revision,
            document,
            entryEvent,
            "record:" + key,
            await hash(document),
          );
          const result = JSON.parse(row);
          assert(
            result.hash === await hash(document) &&
              result.revision === current.revision + 1,
            "Commit not confirmed",
          );
          meta.project = { revision: result.revision, hash: result.hash };
          ack = { event_id: e.event_id, revision: result.revision, conflict };
        }
      }
      const changed = p.known === null ||
        canonical(meta) !== canonical(p.known);
      let snapshot = "null";
      if (changed || needBase) {
        row ??= await store.read(scope);
        const result = JSON.parse(row);
        meta.project = { revision: result.revision, hash: result.hash };
        const parents = [];
        for (let i = 1; i < 3; i++) {
          const pr = JSON.parse(await store.read(scopes[i]));
          meta[names[i]] = { revision: pr.revision, hash: pr.hash };
          const rules = Object.values(pr.document.entries).filter((e: any) =>
            e.kind === "rule" && e.status === "approved"
          ).map((e: any) =>
            Object.fromEntries(
              ["id", "kind", "text", "status", "certainty", "source"].map(
                (k) => [k, e[k]],
              ),
            )
          );
          const body = { scope: scopes[i], revision: pr.revision, rules };
          parents.push({ ...body, checksum: await digest(body) });
        }
        snapshot = '{"scope":' + canonical(scope) + ',"snapshot":' + row +
          ',"parents":' + canonical(parents) + "}";
      }
      const result = {
        format: "agency-edge-sync/1",
        scope,
        known: meta,
        ack,
        need_base: needBase,
        quota: quotaStatus(config.quota, clock()),
      };
      // Compose raw row, avoiding JS numeric precision/canonicalization changes.
      const raw = canonical(result);
      return new Response(raw.slice(0, -1) + ',"snapshot":' + snapshot + "}", {
        headers,
      });
    } catch {
      // Never log bodies, keys, old memory or DB errors. Same denial for unknown/scope/signature.
      return new Response('{"error":"sync_unavailable","retryable":true}', {
        status: 503,
        headers,
      });
    }
  };
}

/** Least-operation adapter. Privileged key is a trusted Edge boundary, NOT an RLS sandbox. */
export class SupabaseMemoryStore implements Store {
  constructor(
    private url: string,
    private key: string,
    private pilot: Scope,
    private fetcher: typeof fetch = fetch,
  ) {
    assert(/^https:\/\/[a-z0-9]+\.supabase\.co$/.test(url));
    assert(key.length > 20);
  }
  private scope(s: Scope, write = false) {
    assert(
      s.owner_id === this.pilot.owner_id &&
        (write
          ? canonical(s) === canonical(this.pilot)
          : canonical(s) === canonical(this.pilot) ||
            (s.project_id === null && s.workspace_id === "owner" &&
              ["*", this.pilot.agent_id].includes(s.agent_id))),
      "Scope unavailable",
    );
  }
  private params(s: Scope) {
    this.scope(s);
    return {
      owner_id: "eq." + s.owner_id,
      agent_id: "eq." + s.agent_id,
      workspace_id: "eq." + s.workspace_id,
    };
  }
  private async call(
    table: string,
    params: Record<string, string>,
    body?: string,
  ) {
    assert(
      [
        "agent_memory_workspaces",
        "agent_memory_revisions",
        "content_factory_projects",
        "rpc/agent_memory_commit",
      ].includes(table),
    );
    const response = await this.fetcher(
      this.url + "/rest/v1/" + table + "?" + new URLSearchParams(params),
      {
        method: body ? "POST" : "GET",
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
    assert(response.ok, "Store unavailable");
    const raw = await response.text();
    assert(bytes(raw).length < 3_000_000, "Store response too large");
    return raw;
  }
  async metadata(s: Scope): Promise<Meta> {
    this.scope(s);
    if (s.project_id !== null) {
      const projects = JSON.parse(
        await this.call("content_factory_projects", {
          id: "eq." + s.project_id,
          owner_id: "eq." + s.owner_id,
          select: "id",
          limit: "1",
        }),
      );
      assert(projects.length === 1, "Project unavailable");
    }
    const binding = JSON.parse(
      await this.call("agent_memory_workspaces", {
        ...this.params(s),
        select: "project_id",
        limit: "1",
      }),
    );
    assert(
      binding.length === 0 || binding[0].project_id === s.project_id,
      "Workspace binding mismatch",
    );
    const rows = JSON.parse(
      await this.call("agent_memory_revisions", {
        ...this.params(s),
        select: "revision,hash",
        order: "revision.desc",
        limit: "1",
      }),
    );
    const m = rows[0] ?? { revision: 0, hash: await hash(emptyDocument) };
    assert(integer(m.revision) && /^[a-f0-9]{64}$/.test(m.hash));
    return m;
  }
  async read(s: Scope) {
    this.scope(s);
    return firstRow(
      await this.call("agent_memory_revisions", {
        ...this.params(s),
        select: "revision,document,event,hash,idempotency_key,created_at",
        order: "revision.desc",
        limit: "1",
      }),
    ) ??
      '{"revision":0,"document":' + emptyDocument + ',"hash":"' +
        await hash(emptyDocument) + '"}';
  }
  async commit(
    s: Scope,
    expected: number,
    document: string,
    event: any,
    key: string,
    docHash: string,
  ) {
    this.scope(s, true);
    const args = {
      p_owner: s.owner_id,
      p_agent: s.agent_id,
      p_workspace: s.workspace_id,
      p_project: s.project_id,
      p_expected: expected,
      p_event: event,
      p_key: key,
      p_hash: docHash,
    };
    const raw = canonical(args);
    const row = firstRow(
      await this.call(
        "rpc/agent_memory_commit",
        {},
        raw.slice(0, -1) + ',"p_document":' + document + "}",
      ),
    );
    assert(row);
    return row;
  }
}
