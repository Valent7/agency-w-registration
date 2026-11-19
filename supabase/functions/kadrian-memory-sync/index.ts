import { binding } from "./binding.ts";
import { handler } from "./service.ts";
import { GatedMemoryStore } from "./gate.ts";
// Environment supplies connection credentials ONLY. It cannot enable the gate,
// change the release/device/scope, or grant approval. Missing binding => closed.
const closed = () =>
  new Response('{"error":"pilot_disabled"}', {
    status: 503,
    headers: { "Cache-Control": "no-store" },
  });
let serve: (r: Request) => Promise<Response> | Response = closed;
if (binding) {
  try {
    serve = handler(
      { enabled: true, ...binding },
      new GatedMemoryStore(
        binding.url,
        Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
        binding.pilot,
        binding.release,
      ),
    );
  } catch { /* malformed connection also fails closed */ }
}
Deno.serve(serve);
