import { Config, handler, SupabaseMemoryStore } from "./service.ts";
// No shared agent imports, startup hooks, cron, background tasks or deploy triggers.
// New function-specific configuration only; default closed until explicitly enabled.
const config: Config = JSON.parse(
  Deno.env.get("KADRIAN_MEMSYNC_CONFIG") ?? '{"enabled":false}',
);
const serve = config.enabled
  ? handler(
    config,
    new SupabaseMemoryStore(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
      config.pilot,
    ),
  )
  : () =>
    new Response('{"error":"pilot_disabled"}', {
      status: 503,
      headers: { "Cache-Control": "no-store" },
    });
Deno.serve(serve);
