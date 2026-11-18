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
