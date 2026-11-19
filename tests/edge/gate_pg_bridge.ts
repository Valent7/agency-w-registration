// Synthetic local stdio PostgREST substitute; no network or production credentials.
import { handler } from "../../supabase/functions/kadrian-memory-sync/service.ts";
import { GatedMemoryStore } from "../../supabase/functions/kadrian-memory-sync/gate.ts";
import { request } from "./support.ts";
const lines = Deno.stdin.readable.pipeThrough(new TextDecoderStream());
const reader = lines.getReader();
let buffer = "";
async function line(): Promise<string> {
  while (!buffer.includes("\n")) {
    const r = await reader.read();
    if (r.done) throw Error("closed");
    buffer += r.value;
  }
  const at = buffer.indexOf("\n");
  const s = buffer.slice(0, at);
  buffer = buffer.slice(at + 1);
  return s;
}
function emit(value: unknown) {
  Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify(value) + "\n"));
}
const fixture = JSON.parse(await Deno.readTextFile(Deno.args[0]));
let tail = Promise.resolve();
const fetcher = (async (_url: any, init: any) => {
  const previous = tail;
  let unlock!: () => void;
  tail = new Promise<void>((r) => {
    unlock = r;
  });
  await previous;
  try {
    emit({ rpc: init.body });
    const reply = JSON.parse(await line());
    return new Response(reply.raw, { status: reply.status });
  } finally {
    unlock();
  }
}) as typeof fetch;
let serverNow = 1000;
const serve = handler(
  {
    enabled: true,
    pilot: fixture.scope,
    devices: {
      synthetic: { scope: fixture.scope, public_key: fixture.public_key },
    },
  },
  new GatedMemoryStore(
    "https://synthetic.supabase.co",
    "synthetic-not-real-server-credential",
    fixture.scope,
    fixture.release,
    fetcher,
  ),
  () => serverNow,
);
while (true) {
  const e = JSON.parse(await line());
  serverNow = e.now;
  const r = await serve(request(e.envelope));
  emit({ done: true, status: r.status, raw: await r.text() });
}
