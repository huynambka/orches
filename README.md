# Orches

Orches is a local workspace that runs user-defined agent pipelines against your own project folders.
You draw a pipeline as a graph of agent nodes, give each node a model and a prompt, then run it: one
root orchestrator agent drives the graph through host-enforced tools while the host enforces
dependency order.

## Prerequisites

- Python 3.11+
- `omp` on `PATH` with provider credentials configured
- Node 22.22.3+ / 24.15+ for the web client

Install the package in a virtual environment, then copy `config.example.toml` to `orches.toml` if you
want to change the root orchestrator model, storage paths, or the `omp` binary.

## Local workspace

```text
python -m pip install -e ".[web]"
orches api --host 127.0.0.1 --port 8000
```

In a second terminal, start the Nuxt client:

```text
cd web
npm install
npm run dev
```

The client is available at `http://localhost:3000`.

1. **Profiles** — create a profile, add agent nodes, drag them, connect dependencies, and select a
   node to set its name, model and prompt. The model list comes from `omp models --json`; if OMP
   cannot provide that catalog, the editor reports the error instead of inventing models.
2. **Projects** — create a project with a name and an existing local folder, write the objective and
   initial brief, and select a profile. Settings must be saved before a run starts.
3. **Manage agents** — the run graph shows the root orchestrator plus every profile node, live status,
   logs and chat. Node drags are saved per project through `PATCH /api/projects/{project_id}/agent-layout`.

Each run snapshots the selected profile, so later profile edits never change a run already in flight
or its history. A run contains one root invocation plus one invocation per configured node; the root
may run independent ready nodes concurrently, but a node cannot be duplicated or started before its
dependencies complete. Running projects cannot be edited or started twice.

Worker nodes run headless with file tools only (`read`, `write`, `edit`, `glob`, `grep`, `todo`,
`web_search`); shell/exec tools are not available to them. If any worker fails, the pipeline can no
longer complete: the host refuses to start further workers, lets in-flight workers finish, gives the
root a short grace window to wrap up, then aborts it, and the run ends `blocked` with the worker's
error instead of idling until the root timeout.

A finished run (`completed`, `failed` or `blocked`) can be **resumed**: the Manage agents page offers
**Resume run**, which starts a new run from the same profile snapshot and carries over every worker
that completed (its output, logs and status), so only failed and unstarted workers execute again.
Selecting a worker and choosing **Rerun from here** carries over everything except that worker and
its dependents. Carried-over workers show "reused from previous run"; the root reads their saved
output and cannot start them again. The previous run is replaced, as with any new run.

The root orchestrator is not a worker node: it cannot be moved, connected or deleted, but selecting
it lets you pick its **model** (`root_model`, defaulting to `[roles.root] model_selector` in
`orches.toml`) and an **Orchestrator prompt** (`root_prompt`) that is appended to the built-in
orchestration and safety prompt on every later run. Both are saved with the profile and captured in
each run's snapshot; the prompt never replaces the host-enforced tool contract or dependency rules.

For a one-command local workspace on Windows, run `.\start-monitor.ps1` from the repository root. The
launcher checks that both ports are free and confirms the API is up before opening the browser.

## Commands

```text
orches api --host 127.0.0.1 --port 8000
orches projects list --json
orches run start <project_id> --dry-run --json
orches run start <project_id> --json
orches run show <run_id> --json
```

## Connections

The **Connections** tab is an address book of machines you work with: name, OS, IP and an optional
credential. Pipeline agents can use it through host tools: the root and every worker get
`read_connections` (masked list with live usage, also included in the run context),
`claim_connection` / `release_connection` (register exclusive use of one connection for the
calling agent), and `reveal_connection` (returns the stored credential, only to the agent that
claimed it). A claim is exclusive: while one agent is using a connection, other agents cannot
claim or reveal it, and the UI blocks editing or removing it (409). Claims end when the owner
releases them or automatically when the owning agent invocation or its run ends — including
cancellation and API-restart recovery. Orches itself never connects to those machines.
Credentials are stored in plaintext in the local SQLite database and are returned by the agent
reveal tool and the explicit local-only reveal route.

## Storage

- Projects, runs and connections: `.orches/state.sqlite3` under the repository (or `[runtime] state_db`).
- Run artifacts and logs: inside the bound project folder under `.orches/runs/<run_id>`, with agent
  sessions under `.orches/sessions/<run_id>`. A `.orches/project.json` marker binds a folder to one project.
- Reusable profiles: `~/.orches/profiles.sqlite3` (including `%USERPROFILE%\.orches` on Windows).

Data written by the previous product is discarded on first start: old projects, runs,
generated run/session files and fixed-role profiles are removed, while saved connections and every
file you created in a project folder are preserved.

Removing a project from Orches deletes only its records; the project folder and its files are left
untouched. Agent output is redacted for common secret patterns before it is stored as an artifact.
