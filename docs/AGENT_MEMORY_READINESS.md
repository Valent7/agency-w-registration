# Readiness record (preparation only)

Base: main 2ae3af661a0c5cae062bdd08d94b39f07f5dbd58.
Branch: codex/agency-agent-memory-v1.

No production install, SQL execution against Supabase, Render deployment, key
change or paid media/model generation was performed. Only code, synthetic tests
and deployment-independent documentation belong in this branch.

Local tests (macOS, Python 3.12): new memory behavior/security suite and existing
Kadrian tests are run separately. Full regression initially reports missing DejaVu
fonts/media capabilities in this local environment; this is not a passing full
regression. Ubuntu CI installs those dependencies and runs the full suite.
The dedicated PostgreSQL job tests SQL grants, real transactions, idempotency and
append-only history using an ephemeral localhost database. Results must be checked
on the final commit before describing preparation as fully verified.

Production acceptance still requires separately authorized verification of the
correct Supabase project, actual grants/backup capacity and authenticated owner
identity. Other agents have a reusable core, not automatic deployment or implicit
access to Kadrian data. Local bootstrap instructions remain unchanged until reviewed.

## First GitHub verification

On implementation commit 998eea4bb4b49017f081f7b7b27fc1acc5ac6882 both jobs passed:
- Agent memory preparation tests, run 44326036982 (real PostgreSQL included).
- Content factory autopilot, run 44326036375 (full Ubuntu/media regression).

A subsequent review adds durable per-transition worker outboxes, a real Streamlit
owner-approval test, strict backup shape validation and explicit project UUID
knowledge-profile selection. Those changes receive a fresh CI run; prior green
checks are not used as proof for untested changes.

On 84a0de257892d97070f826cdc63443ed8f947982 both jobs also passed:
- Agent memory preparation tests: run 44327394987, 40 tests, no skips in the
  memory suite (including four real PostgreSQL checks); 36 Kadrian tests, two skips.
- Full application/media regression: run 44327395028, success.

Final review additionally supplies scoped plaintext local comparison export and
validated import preview. The full regression workflow now explicitly watches
agent_memory files, so later core changes cannot bypass that regression check.

## Single-project pilot revision

Owner requested a strict one-project gate before installation. The revision adds
one canonical UUID selector AGENCY_MEMORY_V1_PROJECT_ID in addition to the existing
owner allowlist. Missing/invalid/multiple IDs fail closed. All runtime entry points,
UI controls, worker outboxes, snapshots and reconciliation use owner AND project.
Legacy behavior and existing data outside the pilot are preserved.

The Russian operator guide docs/KADRIAN_MEMORY_SYNC_RU.md explicitly distinguishes
implemented automatic behavior from manually initiated exchange. No file watcher,
semantic auto-merge, instruction rewrite or deployed synchronization is claimed.
Validation of this revision is run on the updated PR code; previous green checks
are historical evidence only. Production installation remains unauthorized.
