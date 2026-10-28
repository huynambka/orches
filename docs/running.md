# How to run

## Prerequisites

- Python 3.11+
- `omp` on `PATH` with provider credentials configured (`omp models --json` must work)
- Node 22.22.3+ / 24.15+ and npm for the web client

## Install

```text
python -m venv .venv
.venv\Scripts\activate          # Windows; use source .venv/bin/activate elsewhere
python -m pip install -e ".[web]"
cd web && npm install && cd ..
```

## Configure (optional)

Copy `config.example.toml` to `orches.toml` in the repository root, or pass
`--config <path>` to `orches api`.

```toml
[runtime]
state_db = ".orches/state.sqlite3"   # projects/runs/connections
runs_root = ".orches/runs"           # only used for runs without a bound folder
# profiles_db defaults to ~/.orches/profiles.sqlite3
max_prompt_bytes = 300000
max_output_bytes = 64000000

[omp]
binary = "omp"

[roles.root]            # the root orchestrator; workers are configured per profile node
model_selector = ""     # empty = omp's default model
thinking = "high"
timeout_seconds = 3600
approval_mode = "yolo"
```

Relative paths resolve against the repository root. A profile's `root_model` /
`root_thinking` override `[roles.root]` for that profile.

## Start

Two terminals:

```text
orches api --host 127.0.0.1 --port 8000      # or: python -m orches api ...
```

```text
cd web
npm run dev                                  # http://localhost:3000
```

The web dev server proxies `/api/*` to `http://127.0.0.1:8000`; override with
`NUXT_API_PROXY_URL=http://127.0.0.1:<port>`.

Windows one-liner: `.\start-monitor.ps1` opens both in separate PowerShell windows, waits
for the API's `/openapi.json`, then opens the browser. Options: `-ApiPort`, `-WebPort`,
`-Config <path>`, `-NoBrowser`.

Restart the API after pulling backend changes: schema upgrades run at startup, and run
tasks live inside the API process (runs started by a previous process are marked failed
on restart).

## CLI

```text
orches projects list --json
orches run start <project_id> --dry-run --json   # plan only, no omp needed
orches run start <project_id> --json             # runs in the foreground
orches run show <run_id> --json
```

CLI runs are recorded with `source = "cli"`; the web UI shows them but cannot cancel
them or chat with their agents (those need the API process that owns the session).

## Tests and checks

```text
python -m unittest discover -s tests             # backend (70 tests)
cd web
npm test                                         # vitest
npm run lint
npm run typecheck                                # vue-tsc; a volar plugin warning is expected noise
npm run build
```

Backend tests never call the real `omp`; RPC paths use a stdio fake child. For a browser
smoke test against your own data, run a second API on another port with a config whose
`state_db` / `profiles_db` point at copies, and `NUXT_API_PROXY_URL` at it.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Model select shows an error | `omp models --json` failed; fix omp credentials. The UI never invents models. |
| `HTTP 409` on start | Unsaved settings, no objective, empty profile, or a run already active. |
| `HTTP 400: role is not part of the selected pipeline profile` | Node layout saved for a node of another profile; reload the page. |
| Run ends `blocked` | A worker failed, or the root ended without `finish_pipeline`; see the root's Diagnostics tab and `report.json`. |
| Chat disabled for an agent | Only `running` agents owned by this API process accept guidance. |
| Port already in use | `start-monitor.ps1` refuses to start; close the old windows or pass other ports. |
