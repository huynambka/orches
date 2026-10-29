# Orches — session handoff

Context file for continuing work in a new chat. Everything below was implemented and
verified in the previous session(s); treat it as ground truth about the repo at
`D:\Code\idea\orches`.

## What the product is

**Orches** is a local workspace that designs and runs
user-defined AI agent pipelines against local project folders. A fixed **root
orchestrator** agent drives a user-drawn DAG of **agent nodes** through host-enforced
tools (`read_profile`, `get_status`, `run_agent`, `read_agent_output`,
`finish_pipeline`); the host enforces dependency order and safety.

## Stack

- **Backend**: Python 3.11+, FastAPI + uvicorn, SQLite. Package `orches/`, CLI `orches`
  (`api`, `run start|show`, `projects list`). Agents run via the `omp` CLI in RPC mode
  (`orches/omp.py` — `OmpClient` / `OmpRpcSession`).
- **Frontend**: Nuxt 4 + Vue 3 + TypeScript in `web/`, plain CSS design tokens
  (`web/app/assets/css/main.css`), Vue Flow (`@vue-flow/core` + background/controls/minimap).
  Fonts: Google Sans Flex (sans) + Google Sans Code (mono), one Google Fonts link in
  `nuxt.config.ts`. No React/Tailwind/shadcn.
- **Storage**: repo-root `.orches/state.sqlite3` (projects/runs/invocations/artifacts/
  connections), `~/.orches/profiles.sqlite3` (reusable profiles), run artifacts inside each
  project folder under `.orches/runs/<run_id>`.

## Domain model & API (local-only, `/api/*`)

- **Profile** (`orches/pipeline_profiles.py`): `{id, name, steps[], root_prompt?, root_model?, root_thinking?}`;
  step = `{id: node_<32hex>, name, model_selector, prompt, depends_on[], position, thinking}`.
  `thinking` is the omp `--thinking` level for that agent's session
  (`off|minimal|low|medium|high|xhigh|max|auto`, default `medium`; optional on input so
  older saved profiles load). Recorded on the invocation and shown in the run Details tab.
  Limits: ≤64 nodes, names ≤160, prompts ≤10000, selectors ≤256, positions ±10000, DAG only.
  Revisioned optimistic save: `PUT /api/pipeline-profiles/{id}` with `revision` → 409 on stale.
  `root_model` (empty = `[roles.root]` default from `orches.toml`) picks the orchestrator model;
  `root_thinking` (same levels as step `thinking`, empty = `[roles.root] thinking`) its effort.
- **Project**: name + existing bound folder (marker `.orches/project.json`), objective
  (`target`), `initial_prompt`, selected profile. Start gates: saved draft, objective,
  profile with ≥1 agent, no active run. One current run per project (old terminal runs pruned).
- **Run**: immutable profile snapshot, root + one invocation per node, 2s polling from UI,
  per-agent stdout conversation / stderr logs (`/logs`), guidance chat to running agents
  (`/messages`, gated by `chat_available`), per-project node layout persistence.
  **Cancel**: `POST /api/runs/{run_id}/cancel` (202) cancels the API-owned run task →
  run `failed` / error `run cancelled`; 404 unknown, 409 not running / not managed by this
  API process (e.g. CLI runs).
  **Resume**: `POST /api/runs/{run_id}/resume` (202, body optional `{"rerun": [node_ids]}`) on a
  terminal run → new run of the project's **currently saved profile** (`_selected_profile`, shared
  with Start; 409 if none/missing/empty); completed workers are carried over only when
  `PipelineProfile.unchanged_nodes(snapshot)` says their model/prompt/thinking/depends_on match
  the finished run and their whole dependency chain is reusable (`reusable_nodes`), minus `rerun` nodes and
  their dependents. Carry-over = copy `raw/<node>.*` into the new run (artifacts re-registered)
  + `StateStore.carry_over_invocations` (`prompt_version='reused'`, `output_json`) + controller
  seeded via `Orchestrator.run(completed_outputs=…)` so `get_status` reports them completed,
  `read_agent_output` returns the saved output (`reused: true`) and `run_agent` refuses them;
  root context carries `resumed_from_run`. Capture happens before `reserve_project_run` deletes
  the old run. Worker final output is persisted as `agent_invocations.output_json` (schema v3,
  additive `ALTER TABLE` upgrade). 409 if the source run is still running; 400 unknown rerun ids.
- **Connections**: address book (name/os/ip/cred); cred masked in all API list/mutation
  responses, explicit reveal route only. Agents access it via host tools: root + workers
  get `read_connections`/`claim_connection`/`release_connection`/`reveal_connection`
  (`orches/connection_tools.py`); masked list is embedded in run context (`project_context`);
  workers need the per-run `worker-approvals.yml` `--config` overlay because host tools
  default to exec tier and `write` mode would deny them. **Exclusive claims**: a
  `connection_claims` row (SQLite, `orches/state.py`) binds a connection to one running
  `agent_invocations.id`; claim/reveal/release require the live owner (root binds via
  `on_invocation_started` → `PipelineController.bind_root_invocation`); competing agents get
  clean `_is_error` denials; claims auto-release in the same transaction as terminal
  invocation/run updates and `fail_interrupted_api_runs()`; claimed records reject
  PATCH/DELETE with 409; `in_use`/`used_by` exposed in API + Connections page Usage column.
- **Agent inbox** (`orches/agent_messages.py`, schema v5 `agent_messages`): worker ↔ root only
  (worker→worker refused by `StateStore.post_agent_message`). Workers get `send_agent_message(text)`
  (no `to`; always the root), root gets `send_agent_message(to=<worker id>, text)`; both get
  `reply_agent_message(message_id, text)` and `read_agent_messages(after_id?, wait_seconds? ≤10)`
  (`WORKER_MESSAGE_TOOL_DEFINITIONS` / `ROOT_MESSAGE_TOOL_DEFINITIONS`). Workers learn this from
  `_WORKER_SYSTEM`, not from profile prompts. Asynchronous: send returns an id; the recipient reads
  its inbox and replies to that id; replies go back to the asker only (no redirect/forgery; one reply
  per question). Both parties must be `running` in the same running run; text ≤4000 chars, redacted
  via `ArtifactStore.redact_text`. One `AgentMailbox` per run (`Orchestrator.run`),
  `asyncio.Condition` wakes waiting reads. Caller identity is the host-bound invocation id (root via
  `bind_root_invocation`, workers via the `message_handler` closure in `AgentRunner.run_agent`); the
  worker approvals overlay is `WORKER_HOST_TOOL_APPROVALS_YML`.
  To make this possible **`run_agent` is now nonblocking**: it returns `{"id","status":"running"}`
  and the root polls `get_status` / `read_agent_output`; worker failure/fatal marking moved into
  `PipelineController._execute`. Inbox rows cascade with the run (Resume starts empty).
- **MCP server catalog** (`orches/project_mcp.py`, `orches/mcp_schema.json`, schema v6 `mcp_catalog` +
  `project_mcp_sources`): central reusable server configurations (catalog CRUD in StateStore) and per-project
  copies saved to OMP-native `<project>/.omp/mcp.json`. Catalog edits never auto-update projects; explicit
  **Fetch newest config** per entry. Validation uses vendored OMP JSON schema via `jsonschema`; names match
  OMP's `^[a-zA-Z0-9_.-]{1,100}$`. File mutations require `if_revision` (SHA-256) for conflict detection;
  symlinked `.omp` or out-of-project paths rejected. `ProjectMcpService` coordinates file writes + DB
  provenance under StateStore transaction. OMP launches now pass `--cwd <project>` instead of `--add-dir` so
  OMP discovers `.omp/mcp.json` natively; worker workspace becomes an `--add-dir`. Built-in tool restrictions
  unchanged (`--tools`/`--no-tools` do not set SDK `restrictToolNames`, so MCP discovery stays enabled).
  On-demand project connectivity probe via `OmpClient.probe_mcp`: ephemeral RPC session sends `/mcp test <name>`,
  parses `command_output`, never calls a model. UI: `/mcp-servers` catalog CRUD page (no status column),
  project setup sidebar MCP section with source badges, status display, Check/Fetch/Add/Edit/Remove actions.
- **Authentication** (`orches/auth.py`): single admin password from `ORCHES_ADMIN_PASSWORD`
  (≥16 chars; API refuses to start without it). `AdminSessions` keeps PBKDF2 verifier + SHA-256
  token digests in-process (12 h expiry, 128-session cap, 5-failures/60 s per-peer login limit →
  429). HTTP middleware 401s everything except `/api/health`, `POST /api/auth/login`, OPTIONS;
  OpenAPI/docs disabled; CORS allows exactly `ORCHES_WEB_ORIGIN` with Authorization header;
  `_require_local` removed (bearer auth covers remote use). Secret is scrubbed from all child
  envs (`start_session`, `probe_mcp`, `omp models`, launcher web children). Web: bearer token in
  per-tab `sessionStorage` (`orches-admin-token`), `/login` page, `auth.global.ts` middleware,
  401 → clear token + redirect (handler set in `plugins/api-config.client.ts`), Log out in topbar
  and editor toolbar. Direct browser→backend via `NUXT_PUBLIC_API_BASE_URL` (bare https origin or
  http loopback), else same-origin Nuxt proxy. Tests: `tests/test_auth.py`; API fixtures log in
  through the real route.
- **Models**: `GET /api/omp-models` shells out to `omp models --json` (chat models only).
- **Costs** (`orches/costs.py`, schema v4 `model_cost_events`): `OmpRpcSession` calls an optional
  `usage_event_handler(event, index)` for every public `message_end` / `cache_warming_end` as it
  streams (not at `agent_end`, which may omit already-streamed messages); `AgentRunner` wires
  it for root + workers to `StateStore.record_model_usage(invocation_id, parse_cost_event(...))`.
  Ledger rows are project-owned (FK cascade on project delete, **no** FK to runs/invocations so
  pruning and Resume clones leave them intact; reused workers add no rows), keyed by
  `(invocation_id, event_key)` where `event_key` is `message:<messageId>` or `warming:<index>`.
  `model_key` = resolved `provider/model`, else configured selector, else `Unknown model`.
  `cost_usd` is omp's `usage.cost.total` (estimate or server-reported); missing → NULL =
  unpriced, never 0. `GET /api/costs` → `{rows}` per project × model; `/api/runs/{id}` agents carry
  `usage` (null for reused / no completed request). UI: `/costs` page (`lib/costs.ts` sums with
  null-preserving semantics, `(partial)` when unpriced requests exist), Costs nav entry, agent
  Details tab block. Tracking starts from this version; no backfill from old logs.

## Key behaviors / fixes landed (don't regress)

1. `OmpRpcSession._record_stream_error`: pipe `ConnectionError`/`OSError` **after** terminal
   `agent_end` is ignored (was: "Connection lost" failed finished runs). Before terminal →
   still an error. Tests: `tests/test_omp_session.py`.
2. Worker handoff = final assistant text only (`AgentResult.output.text`), not the raw JSONL
   event log (was: blew the 300KB prompt limit of downstream nodes).
3. Workers run with `approval_mode="write"`, timeout 600s (`agent_runtime.run_agent`);
   worker system prompt says shell is denied (headless RPC auto-denies approval requests).
4. Subprocess stream `limit=max(64KiB, min(max_output_bytes, 64MB))` (single JSONL events
   can exceed asyncio's 64KiB readline default). Default `max_output_bytes` now 64MB
   (`orches/config.py`, `config.example.toml`).
5. Frontend: all `window.confirm/prompt/alert` replaced by `ConfirmDialog.vue`
   (promise-based native `<dialog>`); link-styled `.control` anchors are inline-flex
   centered; profiles page is a card grid (`page-shell-narrow`, chips, Edit/Delete);
   light/dark/system theme (`localStorage orches-theme`), collapsible sidebar
   (`orches-sidebar-collapsed`), mobile drawer; profile editor is a full-height workbench
   at `/profiles?edit=<id>` (palette drag/drop, minimap, auto-layout `layoutProfileSteps`,
   undo/redo 50 snapshots, client validation `validateProfileDraft`, 409 conflict
   reload/keep-draft, dirty guards); run page shows honest profile **preview** when no run
   exists (no fabricated status), details dock tabs Conversation/Diagnostics/Details,
   **Stop run** / **Resume run** in the project page header with confirm dialogs; **Rerun from here**
   (agent dock, non-root) calls `/resume`. The project page (`pages/projects/[projectId]/index.vue`,
   the former `/agents` route is merged into it: run workspace left, sticky "Project setup"
   sidebar right) shows a "Saved profile changed" banner (`lib/profileDiff.ts` `profileDrift`:
   root model, per-node model/prompt/effort/dependency changes, added/removed nodes, different
   profile id) comparing `run.pipeline_profile` (snapshot) with `project.pipeline_profile_definition`.
   **Canvas nodes (both editor and run/preview graphs) are 64×64 icon-only squares**
   (`NodeRoleIcon.vue`: root = branch icon, agent = person icon; names/status only in
   `.sr-only` text). Details appear only after clicking a node: editor inspector,
   `AgentWorkspace` run panel (`selected` starts `null`, "No node selected" placeholder) and
   the no-run preview panel on the project page (saved model/effort/prompt/dependencies).
6. Orchestrator model selectable per profile: root node inspector → "Orchestrator model"
   select (`root_model`), recorded on root invocation and in run snapshot.
7. Stuck-pipeline hardening (verified live against real omp): worker tool failures are
   clean denials (approval `select` → `extension_ui_response cancelled` → deny), so a
   lone denied `bash` never hangs. The real stuck risk was an unwinnable pipeline: a
   failed worker + "continue until all complete" prompt + 3600s root timeout. Now:
   workers advertise only non-exec tools (`WORKER_TOOLS` in `agent_runtime.py`); on
   worker failure the controller sets `fatal` and refuses new `run_agent`/
   `finish_pipeline`; `Orchestrator._fatal_watchdog` drains in-flight workers, waits
   `FATAL_ROOT_GRACE_SECONDS` (60s), sends RPC `abort` (new `OmpRpcSession.abort()`),
   then closes after `FATAL_ABORT_GRACE_SECONDS` (15s); run error reports the worker
   failure (fatal checked before root status). `finish()` now reads `stopReason` from
   the last `agent_end` message (`_terminal_stop_reason`) — top-level check was dead.

## Verification harness

- Backend: `python -m unittest discover -s tests` (55 tests). `tests/test_costs.py` includes an
  RPC capture test that patches `asyncio.create_subprocess_exec` to launch a temp stdio Python
  child speaking the RPC protocol through the real `OmpClient`/`AgentRunner` path.
- Frontend (`web/`): `npm test` (26), `npm run lint`, `npm run typecheck` (volar plugin
  warning is pre-existing noise; exit 0), `npm run build`.
- `tests/fixtures/fake_omp.py`: deterministic omp stand-in (`models --json` + RPC; drives
  root host-tools to completion; `FAKE_OMP_DELAY` env seconds slows workers for cancel tests).
  Smoke pattern: temp dir with `orches.toml` pointing state/runs/profiles at temp paths and
  `[omp] binary` at a `.cmd` wrapper for the fixture; API on :18000, Nuxt on :13000 with
  `NUXT_API_PROXY_URL`. Never point smoke at the user's real DBs.

## Live state at handoff

- API running on `http://127.0.0.1:8000` (latest code incl. cancel), web dev server on
  `http://localhost:3000` (both started by the assistant as background services).
- User workspace contains: project **Notea** (`D:\Code\notea`) with a completed run that
  generated a working localStorage note app (`index.html`, `styles.css`, `app.js` —
  verified in browser; note: scripted title-field typing wasn't verified, body persistence
  was); profile **Website builder** (root `anthropic/claude-opus-4-6`, workers
  `anthropic/claude-haiku-5-5`, Design→Build); plus earlier demo project/profile.
- Cancel was verified end-to-end on an isolated stack: Stop run button → confirm dialog →
  run `failed` / `run cancelled` within a few seconds (allow ~2–5s for omp teardown).

## Known rough edges / candidate next steps

- No per-agent stop, no run history (single current run by design), no streaming (polling).
- Worker timeout fixed at 600s / approval fixed at `write` — not configurable per node.
- `front-end-guide.md` (Orches-specific design guide) and `README.md` are current; keep
  them in sync with UI changes.
- Windows launcher: `start-monitor.ps1` (API 8000 + web 3000).
