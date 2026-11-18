# Agency agent memory v1 — preparation, NOT installed

## Deployment boundary

This branch is an opt-in implementation for review. `AGENCY_MEMORY_V1` defaults to
OFF. No startup function installs SQL, changes credentials, starts a provider,
modifies Render or rewrites legacy projects. Do not enable it before the separate
installation decision and database review. The production database was not used
for development or tests. No tenant files or local Markdown journals belong in Git.

## Existing system and compatibility

The pre-existing `kadrian_chat`, `kadrian_notes`, project facts, job state/checkpoints,
leases and media remain intact. They remain the compatibility read/write path with
the feature disabled. Local AGENTS.md and Agent.md are NOT replaced. There is no
sync daemon that assumes an online desktop. Cloud memory and worker read Supabase
independently; local changes take effect only after controlled exchange.

## Data model and isolation

Generic `agent_memory.py` is independent of Kadrian and of any LLM vendor. It uses:

- Scope: owner ID, agent ID, workspace ID, optional project ID. Project IDs are UUIDs;
  project ownership is checked through the existing store. A workspace cannot be
  rebound to another project. Non-project scopes use workspace `owner`.
- Principal: authenticated actor, owner, kind and exact permitted workspace/agent.
  Agents can read/append their own scope. Verifiers can record evidence and validate
  proposals. Only a trusted owner principal can approve/roll back policy.
- Owner-level scopes: `agent_id=kadrian, workspace_id=owner` for one agent's common
  rules; `agent_id=*, workspace_id=owner` for explicitly approved rules for all agents
  of this owner. No cross-owner global knowledge. For versioned tasks the Agency knowledge profile
is selected by explicit AGENCY_MEMORY_AGENCY_PROJECT_IDS UUID configuration, not a
mutable display name. Only approved rules from these
  scopes are supplied to a project; their raw client knowledge is not inherited.

**Trust boundary:** Principal construction is server-side after existing Agency
authentication/access checks. Never deserialize a principal, permissions or owner
identity from model output, a URL or a sync/backup file. A Python object is not an
authentication token. Existing revocation gates remain required. Other agents must
wire this same boundary in their own authenticated entry points before activation;
this branch integrates Kadrian, it does not silently replace Neona/Ora workflows.

Two additive database tables represent three logical components:
`agent_memory_workspaces` stores immutable project bindings and current revision;
`agent_memory_revisions` stores immutable versioned documents plus their event,
actor/source, idempotency key and digest. Event history, current knowledge and sync
base revisions therefore share one atomic transaction. Full snapshots are a simple
initial implementation, not a vector database. Current document cap is 2 MB;
large deployments will need compaction/archive without deleting event history.
Backups have a separate 64 MB validation ceiling. No embeddings are needed.

`Repository` supports existing SQLite and Supabase stores. SQLite initialization is
explicit for local/testing only. Supabase uses a single `agent_memory_commit` RPC
with row locking, version comparison and operation-key reconciliation. RLS is on;
PUBLIC/anon/authenticated have no access. Server service_role bypasses RLS, so the
Python server authorization checks are essential. RPC is SECURITY INVOKER, with
EXECUTE granted only to service_role. History rejects UPDATE/DELETE. Existing
projects have restrictive FKs, so accidental project deletion cannot cascade memory.

## Evidence and learning cycle

Entries are facts, hypotheses, unknowns, decisions, feedback, analysis, corrections,
checks, rule proposals, execution events or unchanged legacy imports. Certainty
(verified/hypothesis/unknown) is independent of policy approval. Verified claims
require evidence plus an owner/verifier principal. A check has an explicit pass/fail
and an exact rule text hash. Validation requires successful checks linked to the
specific rule. Validation does NOT activate policy. Owner approval records the
actor and time; replacements retain previous versions. Rollback is a compensating
revision that restores the previous approved rule and preserves later tasks/events.

Flow: feedback -> analysis -> correction -> exact rule proposal -> passed check ->
validation -> explicit owner approval -> applied in subsequent task context.
Never infer authorization from the assistant saying it did something. Provider
completion is an execution event, not visual/audio quality evidence. Manual owner
checks are labelled `owner-observation`, not automated tests.

Chat turns are automatically mirrored as unverified execution records. Simple
owner-text patterns also capture tentative feedback/decision records, retaining
exact source text. This does not claim perfect semantic extraction or model
self-training. Other observations can be entered through structured owner controls
or trusted check runners. No extra paid model request is made for extraction.

## Reading memory before actions

`prepare_project` drains durable pending chat events, reads current project memory
and approved owner rules, and stores a task snapshot. Rules and older decisions
are independent of the last chat messages. Context is labelled as data, and cannot
extend tool permissions. `project_context` passes it to chat/planning; image briefs
also receive project context. Worker reads it before provider actions. A changed
approved rule fingerprint stops a pinned job for review rather than silently
changing approved production. A check/fact/event that changes does not itself
rewrite an approved screenplay. The content generator receives versioned context.

Runtime UI and worker are OFF unless `AGENCY_MEMORY_V1=1` and the authenticated
owner is explicitly included in `AGENCY_MEMORY_V1_OWNERS` (comma-separated IDs). With it ON, failure to
read mandatory memory stops a new request before model/provider calls. An absent
schema is an error, not a silent fallback to empty memory. Chat turns are a durable outbox; worker transitions include an explicit outbox
committed with the job state. Pending transitions survive later updates until an
optimistic acknowledgement succeeds. Successful old-store writes remain after
event-delivery failure. Reconciliation replays turns and recent (up to 1000) jobs idempotently.
Legacy notes/facts are imported only through an explicit owner action, as legacy
unknown data, and are never deleted or silently promoted to rules.

Initial limitations: reconciliation scans recent jobs and the full turn list; large
archives need pagination/cursors before a high-volume rollout. Paid external
side effects and memory writes cannot be one database transaction. Provider task
IDs/checkpoints and the existing uncertain-result protections remain authoritative;
failed event delivery must never cause a second provider order. A UI model response
can still be lost if the process dies before its legacy turn is committed; task
input and prepared snapshot are durable before a new model call. Exact-once LLM
response persistence across process death is not claimed.

## Controlled local exchange and backups

`local_bundle(root, scope, base_revision)` reads only Agent.md and context/{PROJECT,
DECISIONS,FEEDBACK}.md. No recursive upload, media, secrets, AGENTS.md or .env files.
Known credentials are rejected; pattern checks are defense-in-depth, not a promise
to detect every possible secret. The owner reviews the allowlisted payload first.

`import_local` checks scope, checksum and base revision, imports one immutable
legacy event, and never activates incoming instructions. A repeated package is
idempotent. A changed cloud version stops the import; no last-write-wins merge.
The owner UI previews the exact incoming scope, base version and file text before
import. It also offers an explicit plaintext `.agent-memory.json` download for
local review through the existing authenticated session; no server credential
is copied to the desktop. This contains private data and is ignored by Git.
`stage_cloud_export` writes a private 0600 versioned package in .memory-exchange.
`apply_reviewed_projection` requires exact reviewed cloud checksum and current
local digest. It atomically updates only context/SYNCED_MEMORY.md, retains the
previous projection under .memory-backups, and never changes AGENTS.md/Agent.md.
Symlink paths and conflicting local writes are rejected. The generated projection
is task data, not permission to change general rules. Bootstrap instructions for
reading this projection must be reviewed separately with the owner before enabling
local automatic use; the actual local instruction files were not changed here.

Backup downloads use Fernet with the installation's existing encryption key; no
key is created/rotated or included in an export. Without that existing key the UI
cannot offer encrypted download. Store backups outside Git with access restrictions.
No scheduled external backup destination has been configured. Retention/restore
ownership must be agreed at installation. These application exports complement,
not replace, a separately verified Supabase database backup.

Restore requires an empty matching workspace (or the same interrupted restore),
verifies checksums/history and is resumable. Prior approvals are retained as
historical evidence but require owner reapproval to become current instructions.
Restored task history is non-executable. Restoring into another owner/agent/workspace
is denied; do not restore an entire old DB over newly created production jobs.

## Future agent execution adapters

The memory API is a vendor-neutral boundary: prepare scoped task with pinned rule
versions -> execute with a bounded principal -> emit execution/check/artifact events.
A future Codex SDK / Agents SDK / Agents API adapter can use this boundary without
owning the only copy of memory. No SDK, remote execution permissions, API keys,
new service or billable model runs are added by this change. Before adding an
adapter, review capability allowlists, cancellation, trace redaction, cost ceilings,
request deduplication and provider resume behavior.

## Test and rollout procedure

Offline: `python -m unittest discover -s tests -p 'test_agent_memory*.py' -v`.
Without an explicit local DSN, real PostgreSQL tests are visibly skipped. CI uses
an ephemeral localhost PostgreSQL 17 container with synthetic roles/projects and
no production credentials. DSN guard refuses remote hosts or other DB names.
Run existing Kadrian tests and full offline/media regression as well.

Installation is NOT authorized by this branch. Before any installation:
1. Identify the actual production Supabase project via trusted operator access;
   verify grants, backup/recovery and existing server owner authentication.
2. Review SQL and restore test results. Run the SQL only after separate permission.
3. Back up old notes/chat and code reference privately. Leave feature disabled.
4. Deploy only after separate approval. Enable one owner/project through a reviewed
   rollout using the explicit owner allowlist AGENCY_MEMORY_V1_OWNERS.
5. Import legacy content, verify cloud/desktop round-trip, offline desktop operation,
   concurrent edits, revoked access and paid-operation protections with mocks.
6. Expand only after owner acceptance.

Rollback: set AGENCY_MEMORY_V1 off, roll back code if necessary, leave additive
memory tables intact, use unchanged legacy fields. No DROP/TRUNCATE migration is
provided. Disable new writes before a database restore. Keep newer production jobs.

Cost: no new paid service is required. Existing storage/worker/API usage can grow;
check capacity before enabling. CI uses ordinary repository Actions quota and an
ephemeral test container. No provider credentials are used by these tests. Real
SDK/model-based enrichment would have a separate API cost and is not enabled.
