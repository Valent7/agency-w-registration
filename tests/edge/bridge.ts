// Synthetic stdio bridge: no TCP listener, real Supabase, device Keychain or paid API.
import { handler } from "../../supabase/functions/kadrian-memory-sync/service.ts";
import { canonical } from "../../supabase/functions/kadrian-memory-sync/protocol.ts";
import { FakeStore, request } from "./support.ts";
const fixture = JSON.parse(await Deno.readTextFile(Deno.args[0]));
const store = new FakeStore();
for (const [scope, row] of fixture.rows ?? []) store.rows.set(scope, row);
const config = {
  enabled: true,
  pilot: fixture.scope,
  devices: {
    [fixture.device_id]: {
      scope: fixture.scope,
      public_key: fixture.public_key,
    },
  },
};
const serve = handler(config, store, () => 1000);
let buffer = "";
for await (
  const chunk of Deno.stdin.readable.pipeThrough(new TextDecoderStream())
) {
  buffer += chunk;
  let at;
  while ((at = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, at);
    buffer = buffer.slice(at + 1);
    let result;
    try {
      const msg = JSON.parse(line);
      if (msg.control === "seed") {
        store.rows.set(canonical(msg.scope), msg.row);
        result = { ok: true };
      } else if (msg.control === "fault") {
        store.lost = !!msg.lost;
        store.fail = !!msg.fail;
        result = { ok: true };
      } else if (msg.control === "stats") {
        result = {
          reads: store.reads,
          metas: store.metas,
          writes: store.writes,
          rows: Array.from(store.rows.entries()),
        };
      } else {
        const r = await serve(request(msg));
        result = { status: r.status, raw: await r.text() };
      }
    } catch {
      result = { bridge_error: true };
    }
    Deno.stdout.writeSync(
      new TextEncoder().encode(JSON.stringify(result) + "\n"),
    );
  }
}
