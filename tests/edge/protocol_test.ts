import {
  appendEntry,
  canonical,
  digest,
  emptyDocument,
  hash,
  validateEvent,
} from "../../supabase/functions/kadrian-memory-sync/protocol.ts";
import {
  handler,
  SupabaseMemoryStore,
} from "../../supabase/functions/kadrian-memory-sync/service.ts";
import {
  envelope,
  eq,
  FakeStore,
  keys,
  ok,
  pilot,
  propertyRaw,
  rejects,
  request,
} from "./support.ts";
const event = async (seq = 1, text = "решение") => {
  const e = {
    sequence: seq,
    base_revision: 0,
    files: { "context/DECISIONS.md": text },
    file_modes: {
      "context/DECISIONS.md": {
        mode: "snapshot",
        base_hash: null,
        result_hash: await digest(text),
      },
    },
  };
  return { ...e, event_id: await digest({ device: "synthetic", ...e }) };
};
async function setup() {
  const { k, pub } = await keys();
  const store = new FakeStore();
  const config = {
    enabled: true,
    pilot,
    devices: { synthetic: { scope: pilot, public_key: pub, revoked: false } },
  };
  const serve = handler(config, store, () => 1000);
  const call = async (p: any, c: any = {}) =>
    serve(request(await envelope(k.privateKey, p, c)));
  return { store, config, serve, call };
}
const initial = { known: null, event: null, base_document: null };
Deno.test("canonical Unicode/control strings match Python ordering", () => {
  eq(
    canonical({ "😀": "\n\t\b\f\r", "\uE000": "é" }),
    '{"":"é","😀":"\\n\\t\\b\\f\\r"}',
  );
});
Deno.test("canonical signed payload rejects unsafe numbers/lone surrogates", async () => {
  await rejects(() => canonical(1.5));
  await rejects(() => canonical(9007199254740992));
  await rejects(() => canonical("\uD800"));
});
Deno.test("raw insertion preserves Python numeric lexemes and old fields", () => {
  const raw =
    '{"entries":{"a":{"n":1.0,"s":"a}\\"b"},"z":{"n":1e-07}},"schema":1,"tasks":{"n":9007199254740993}}';
  const out = appendEntry(raw, "m", { n: 3 });
  ok(out.includes("1.0"));
  ok(out.includes("1e-07"));
  ok(out.includes("9007199254740993"));
  ok(
    out.indexOf('"a"') < out.indexOf('"m"') &&
      out.indexOf('"m"') < out.indexOf('"z"'),
  );
});
Deno.test("first poll returns snapshot; idle reads metadata only/no writes", async () => {
  const { call, store } = await setup();
  const first = await (await call(initial)).json();
  ok(first.snapshot);
  store.reads = 0;
  const second = await (await call({ ...initial, known: first.known })).json();
  eq(second.snapshot, null);
  eq(store.reads, 0);
  eq(store.writes, 0);
  ok(canonical(second).length < 1800);
});
Deno.test("unknown, revoked, bad signature, wrong project/agent/owner denied before DB", async () => {
  const { call, store, config, serve } = await setup();
  for (
    const c of [
      { device_id: "absent" },
      { scope: { ...pilot, owner_id: 99 } },
      { scope: { ...pilot, agent_id: "neona" } },
      {
        scope: { ...pilot, project_id: "22222222-2222-4222-8222-222222222222" },
      },
      { issued_at: 0 },
      { operation: "approve" },
    ]
  ) eq((await call(initial, c)).status, 503);
  config.devices.synthetic.revoked = true;
  eq((await call(initial)).status, 503);
  config.devices.synthetic.revoked = false;
  eq((await serve(request({ request: {}, signature: "bad" }))).status, 503);
  eq(store.metas, 0);
  eq(store.writes, 0);
});
Deno.test("disabled pilot fails closed before DB", async () => {
  const { call, config, store } = await setup();
  config.enabled = false;
  eq((await call(initial)).status, 503);
  eq(store.metas, 0);
});
Deno.test("write requires trusted base witness; cannot replace/approve old memory", async () => {
  const { call, store } = await setup();
  const e = await event();
  const noBase = await (await call({ ...initial, event: e })).json();
  ok(noBase.need_base);
  eq(store.writes, 0);
  const result = await (await call({
    known: noBase.known,
    event: e,
    base_document: emptyDocument,
  })).json();
  eq(result.ack.event_id, e.event_id);
  eq(store.writes, 1);
  const entry = Object.values(
    result.snapshot.snapshot.document.entries,
  )[0] as any;
  eq(entry.kind, "legacy");
  eq(entry.certainty, "unknown");
  eq(entry.status, "recorded");
  eq(result.ack.conflict, false);
});
Deno.test("replay after lost ack commits once; different event same sequence rejected", async () => {
  const { call, store } = await setup();
  const e = await event();
  store.lost = true;
  eq(
    (await call({ ...initial, event: e, base_document: emptyDocument })).status,
    503,
  );
  eq(store.writes, 1);
  const result =
    await (await call({ ...initial, event: e, base_document: emptyDocument }))
      .json();
  eq(result.ack.event_id, e.event_id);
  eq(store.writes, 1);
  eq(
    (await call({
      ...initial,
      event: await event(1, "other"),
      base_document: emptyDocument,
    })).status,
    503,
  );
});
Deno.test("stale base refresh then append preserves both versions, conflict recorded", async () => {
  const { call, store } = await setup();
  const old = await (await call(initial)).json();
  const cloud =
    '{"entries":{"cloud":{"kind":"unknown","text":"retained"}},"schema":1,"tasks":{}}';
  await store.seed(pilot, cloud, 4);
  const e = await event();
  const stale = await (await call({
    known: old.known,
    event: e,
    base_document: emptyDocument,
  })).json();
  ok(stale.need_base);
  eq(store.writes, 0);
  const done =
    await (await call({ known: stale.known, event: e, base_document: cloud }))
      .json();
  ok(done.ack.conflict);
  ok(done.snapshot.snapshot.document.entries.cloud);
  eq(Object.keys(done.snapshot.snapshot.document.entries).length, 2);
});
Deno.test("CAS failure retains event/no ack; request retry can recover", async () => {
  const { call, store } = await setup();
  store.fail = true;
  eq(
    (await call({
      ...initial,
      event: await event(),
      base_document: emptyDocument,
    })).status,
    503,
  );
  eq(store.writes, 0);
  store.fail = false;
  ok(
    (await (await call({
      ...initial,
      event: await event(),
      base_document: emptyDocument,
    })).json()).ack,
  );
});
Deno.test("event allowlist denies rules, tasks, secrets and tampering", async () => {
  const e: any = await event();
  for (
    const bad of [{ ...e, files: { "Agent.md": "x" } }, { ...e, sequence: 0 }, {
      ...e,
      event_id: "f".repeat(64),
    }, { ...e, approve: true }]
  ) await rejects(() => validateEvent(bad, "synthetic"));
  await rejects(async () =>
    validateEvent(await event(2, "password=longcredential"), "synthetic")
  );
});
Deno.test("approved parent rules only; no parent evidence or client facts", async () => {
  const { call, store } = await setup();
  const ps = { ...pilot, workspace_id: "owner", project_id: null };
  await store.seed(
    ps,
    canonical({
      schema: 1,
      entries: {
        r: {
          id: "r",
          kind: "rule",
          text: "approved",
          status: "approved",
          certainty: "verified",
          source: "owner",
          evidence: { private: "omit" },
        },
        p: { kind: "rule", status: "proposed", text: "omit" },
        f: { kind: "fact", status: "recorded", text: "omit" },
      },
      tasks: { private: "omit" },
    }),
  );
  const result = await (await call(initial)).json();
  const pr = result.snapshot.parents.find((p: any) =>
    p.scope.agent_id === "kadrian"
  );
  eq(pr.rules.length, 1);
  ok(!canonical(pr).includes("private"));
  eq(pr.rules[0].text, "approved");
});
Deno.test("quota warnings distinguish stale observation from live billing", async () => {
  const { call, config } = await setup();
  let result = await (await call(initial)).json();
  eq(result.quota.status, "unknown_or_stale");
  (config as any).quota = {
    observed_at: 999,
    period_end: 2000,
    invocations: 90,
    included_invocations: 100,
    egress_bytes: 0,
    included_egress_bytes: 100,
  };
  result = await (await call(initial)).json();
  eq(result.quota.status, "warning");
  (config as any).quota.observed_at = 2000;
  eq((await (await call(initial)).json()).quota.status, "unknown_or_stale");
});
Deno.test("adapter binds every query, metadata never selects document, no side services", async () => {
  const calls: any[] = [];
  const store = new SupabaseMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    async (input, init) => {
      const u = new URL(String(input));
      calls.push({
        path: u.pathname,
        query: Object.fromEntries(u.searchParams),
        method: init?.method,
      });
      return Response.json(
        u.pathname.endsWith("content_factory_projects")
          ? [{ id: pilot.project_id }]
          : [],
      );
    },
  );
  await store.metadata(pilot);
  ok(calls.every((x) => !String(x.query.select).includes("document")));
  ok(calls.every((x) => x.query.owner_id === "eq.42"));
  await rejects(() => store.metadata({ ...pilot, owner_id: 55 }));
  await rejects(() =>
    store.commit(
      { ...pilot, project_id: null, workspace_id: "owner" },
      0,
      emptyDocument,
      {},
      "bad",
      "x",
    )
  );
  eq(calls.length, 3);
});
Deno.test("raw RPC body retains old decimal/bigint tokens", async () => {
  let sent = "";
  const doc =
    '{"entries":{},"schema":1,"tasks":{"n":9007199254740993,"f":1.0}}';
  const store = new SupabaseMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    async (_i, init) => {
      sent = String(init?.body);
      return new Response('[{"revision":1,"document":' + doc + "}]");
    },
  );
  await store.commit(pilot, 0, doc, {}, "key", await hash(doc));
  eq(propertyRaw(sent, "p_document"), doc);
});
Deno.test("oversized document fails without truncation", async () => {
  await rejects(() =>
    appendEntry(
      canonical({
        entries: {},
        schema: 1,
        tasks: { x: "x".repeat(1_600_000) },
      }),
      "new",
      {},
    )
  );
});

Deno.test("signed payload tampering is denied cryptographically before DB", async () => {
  const { k, pub } = await keys();
  const store = new FakeStore();
  const serve = handler(
    {
      enabled: true,
      pilot,
      devices: { synthetic: { scope: pilot, public_key: pub } },
    },
    store,
    () => 1000,
  );
  const e = await envelope(k.privateKey, initial);
  e.request.request_id = "b".repeat(32);
  eq((await serve(request(e))).status, 503);
  eq(store.metas, 0);
});

Deno.test("oversized requests are rejected before signature work or database", async () => {
  const { serve, store } = await setup();
  const r = new Request(
    "https://synthetic.supabase.co/functions/v1/kadrian-memory-sync",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "x".repeat(3_000_001),
    },
  );
  eq((await serve(r)).status, 503);
  eq(store.metas, 0);
});
