import { GatedMemoryStore } from "../../supabase/functions/kadrian-memory-sync/gate.ts";
import { handler } from "../../supabase/functions/kadrian-memory-sync/service.ts";
import {
  canonical,
  emptyDocument,
  hash,
} from "../../supabase/functions/kadrian-memory-sync/protocol.ts";
import { envelope, eq, keys, ok, pilot, rejects, request } from "./support.ts";
const release = "a".repeat(32);
Deno.test("gate: each operation goes through sole RPC; no direct table/legacy bypass", async () => {
  const calls: any[] = [];
  const fetcher = (async (url: any, init: any) => {
    ok(String(url).endsWith("/rest/v1/rpc/kadrian_memory_exchange"));
    const body = JSON.parse(init.body);
    calls.push(body);
    return new Response(body.p_action === "begin" ? "12" : "[]");
  }) as typeof fetch;
  // malformed gate response must not be accepted
  const bad = new GatedMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    release,
    fetcher,
  );
  await rejects(() => bad.authorize(pilot, "synthetic"));
  const good = (async (url: any, init: any) => {
    ok(String(url).endsWith("/rest/v1/rpc/kadrian_memory_exchange"));
    const body = JSON.parse(init.body);
    calls.push(body);
    return new Response(body.p_action === "begin" ? '{"generation":12}' : "[]");
  }) as typeof fetch;
  const root = new GatedMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    release,
    good,
  );
  await rejects(() => root.metadata(pilot));
  const session = await root.authorize(pilot, "synthetic");
  await session.metadata(pilot);
  await session.read(pilot);
  for (const c of calls.filter((x) => x.p_action !== "begin")) {
    eq(c.p_generation, 12);
    eq(c.p_release, release);
    eq(c.p_device, "synthetic");
  }
});
Deno.test("gate: warm handler cannot write/read after stop; new epoch rejects stale session", async () => {
  let enabled = true, generation = 1, writes = 0;
  const fetcher = (async (_url: any, init: any) => {
    const p = JSON.parse(init.body);
    if (!enabled || (p.p_action !== "begin" && p.p_generation !== generation)) {
      return new Response("", { status: 403 });
    }
    if (p.p_action === "begin") {
      return new Response(JSON.stringify({ generation }));
    }
    if (p.p_action === "commit") writes++;
    return new Response(p.p_action === "metadata" ? "[]" : "[]");
  }) as typeof fetch;
  const root = new GatedMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    release,
    fetcher,
  );
  const old = await root.authorize(pilot, "synthetic");
  await old.metadata(pilot);
  enabled = false;
  await rejects(() => old.read(pilot));
  await rejects(async () =>
    old.commit(pilot, 0, emptyDocument, {}, "key", await hash(emptyDocument))
  );
  enabled = true;
  generation++;
  await rejects(() => old.metadata(pilot));
  await (await root.authorize(pilot, "synthetic")).metadata(pilot);
  eq(writes, 0);
});
Deno.test("gate: bad signature never reaches even gate RPC; authenticated denied gate returns503", async () => {
  let calls = 0;
  const { k, pub } = await keys();
  const fetcher = (async () => {
    calls++;
    return new Response("", { status: 403 });
  }) as typeof fetch;
  const root = new GatedMemoryStore(
    "https://synthetic.supabase.co",
    "s".repeat(40),
    pilot,
    release,
    fetcher,
  );
  const serve = handler(
    {
      enabled: true,
      pilot,
      devices: { synthetic: { scope: pilot, public_key: pub } },
    },
    root,
    () => 1000,
  );
  const e = await envelope(k.privateKey, {
    known: null,
    event: null,
    base_document: null,
  });
  const tampered = structuredClone(e);
  tampered.signature = "bad";
  eq((await serve(request(tampered))).status, 503);
  eq(calls, 0);
  eq((await serve(request(e))).status, 503);
  eq(calls, 1);
});
