# Neona realtime safety review — 2026-10-13

Offline review only. No Telegram sessions or live contacts were used.

PR #1 has already been merged. The event-driven listener is feature-flagged off by default, so merge alone does not prove reduced production latency.

Risks to address before enabling for all accounts: synchronize FloodWait cooldown across realtime and fallback polling; restrict event sender resolution to approved contacts; test duplicate prevention at the persisted state boundary and across worker processes; measure end-to-end reply latency under safe staging conditions.

Keep Render configuration unchanged until reviewed.