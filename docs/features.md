# Features

## Pipeline profiles

- Visual DAG editor (Vue Flow): square icon-only nodes, click-to-edit inspector, drag
  connections, insert-on-edge, auto-layout, minimap, undo/redo, keyboard shortcuts.
- Per node: name, model (from `omp models --json`), effort (`off … max | auto`), prompt,
  dependencies, position.
- Fixed root orchestrator node with its own model, effort and appended prompt.
- Client-side validation mirroring the backend limits; optimistic revisioned saves (409 on
  stale), dirty guards on navigation.
- Import/Export: per-profile **Export** downloads a portable `*.orches-profile.json`
  (definition only — no ids/revisions); **Import** creates a new profile from such a file
  with freshly generated node ids (dependencies remapped), rolling back on invalid files.
- REST: `GET/POST /api/pipeline-profiles`, `GET/PUT/DELETE /api/pipeline-profiles/{id}`.

## Projects

- Bind a name to an existing folder (marker file; one project per folder).
- Objective, initial brief, selected profile; start gated on saved settings.
- Single page with canvas + setup sidebar; node layout persisted per project
  (`PATCH /api/projects/{id}/agent-layout`).
- List with search, filter, sort, pagination; project cards on small screens.
- REST: `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}`,
  `POST /api/projects/{id}/runs`.

## Runs

- Immutable profile snapshot per run; one invocation per node plus the root.
- Host-enforced execution: dependency order, one start per node, nonblocking worker launch,
  concurrent ready workers, fatal blocking when a worker fails, grace window then abort for
  a root that keeps polling.
- Live status on the canvas; per-agent Conversation (Markdown-rendered replies),
  Diagnostics, Details; 2 s polling.
- **Guidance chat** to running agents over omp RPC `steer`
  (`POST /api/runs/{id}/agents/{inv}/messages`).
- **Stop run** (`POST /api/runs/{id}/cancel`).
- **Resume** and **Rerun from here** (`POST /api/runs/{id}/resume`): completed, unchanged
  workers are carried over with their output and logs; profile drift is detected and shown.
- Artifacts: raw JSONL conversation and stderr per agent, `report.json` per run, all
  secret-redacted; logs paged via `GET /api/runs/{id}/agents/{inv}/logs`.
- Interrupted runs are marked failed when the API restarts; claims are released.

## Agent-to-agent communication

- Worker ↔ root inbox, built into every run; workers learn about it from the system prompt.
- `send_agent_message(text)` (worker → root), `send_agent_message(to, text)` (root → worker),
  `reply_agent_message(message_id, text)`, `read_agent_messages(after_id?, wait_seconds?)`.
- Asynchronous with cursors and bounded waits (≤10 s); replies are bound to the question
  and routed back to its author; one reply per question; finished agents cannot be addressed;
  workers cannot message each other.
- Messages are persisted per run (`agent_messages`), redacted, and dropped with the run.

## Connections

- Address book with name/OS/IP/credential; credential masked everywhere except the explicit
  reveal route and the agent `reveal_connection` tool.
- Exclusive claims per live agent invocation; auto-release on invocation/run end and on
  API-restart recovery; UI blocks edit/delete of claimed entries (409); usage column.
- REST: `GET/POST /api/connections`, `PATCH/DELETE /api/connections/{id}`,
  `GET /api/connections/{id}/cred`.

## Costs

- Per-request ledger of model usage as reported by omp (tokens, USD), keyed by the resolved
  `provider/model`; survives run pruning and resume; deleted with the project.
- `/costs` page by project (expandable per-model rows) and by model; per-agent usage in the
  run Details tab. Unknown prices are shown as unknown, never as zero.
- REST: `GET /api/costs`.

## Platform

- Local-only FastAPI + SQLite backend, `orches` CLI (`api`, `projects list`, `run start|show`,
  `--dry-run`), Nuxt 4 web client with light/dark/system theme, collapsible sidebar, mobile
  drawer.
- Windows launcher `start-monitor.ps1`.
- Configuration via `orches.toml` (`[runtime]`, `[omp]`, `[roles.root]`).
- Automatic schema upgrades; previous-product data discarded on first start (connections kept).

## Not provided

No authentication, no multi-user or remote access, no streaming API, no node types other
than agent nodes, no shell access for workers, no execution history beyond the current run
per project.
