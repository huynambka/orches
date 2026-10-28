# How to use

The workflow is: design a **profile** (the pipeline), bind a **project** (a folder plus a
brief), **run** it, watch and steer the agents, then **resume** or rerun as needed.

## 1. Design a profile (`/profiles`)

1. Click **New profile**, then **Edit** to open the editor workbench.
2. Add agents from the palette (click, or drag onto the canvas). Each node is a square icon;
   click it to open the inspector and set:
   - **Name** — shown to the root and in the run view.
   - **Model** — from `omp models --json`.
   - **Effort** — `omp --thinking` level for that agent's session.
   - **Prompt** — the node's task. It is appended to the built-in worker prompt; it does not
     need to explain host tools (the worker already knows it can ask the root).
3. Connect dependencies by dragging from one agent's handle to another's; the arrow points
   to the dependent agent. Agents without dependencies hang off the root automatically.
   Use **+** on an edge to insert an agent between two nodes, **×** to remove a dependency.
4. Click the root node to set the **Orchestrator model**, **effort** and an optional
   **Orchestrator prompt** (appended to the built-in orchestration prompt).
5. **Validate**, then **Save profile** (Ctrl/Cmd+S). Saving is revisioned: if the profile
   changed elsewhere you get a 409 and can reload or keep your draft.

Other editor controls: Undo/Redo (50 steps), Auto-layout, Fit view, Delete/Backspace
removes the selection, Escape clears it.

Limits: ≤64 nodes, names ≤160 chars, prompts ≤10 000 chars, no cycles.

## 2. Bind a project (`/`)

Click **New project**, give it a name and an **existing** local folder. A folder can be bound
to one project only; Orches writes a `.orches/project.json` marker and keeps run artifacts
under `<folder>/.orches/`.

## 3. Configure and start (`/projects/<id>`)

The project page shows the pipeline canvas on the left and the **Project setup** sidebar on
the right:

- **Objective** — what this run should accomplish (required).
- **Initial brief for agents** — context and constraints, passed as structured data.
- **Pipeline profile** — the profile to run.

**Save settings**, then **Start pipeline →**. The button is disabled, with a reason, while
settings are unsaved, no objective/profile is set, the profile has no agents, or a run is
already active.

Before the first run the canvas is a **preview** of the saved profile: click a node to see its
saved configuration. Drag nodes to arrange them; positions are saved per project.

## 4. Watch and steer a run

The canvas shows the root plus every profile node with a status colour. **Click a node** to
open its panel:

- **Conversation** — the agent's stdout conversation (assistant text, tool calls, results).
- **Diagnostics** — stderr.
- **Details** — invocation id, model, effort, timestamps, return code, artifacts, model
  usage for this run, and the run's stage list.
- **Guide …** — send guidance to a *running* agent. It is delivered as an omp `steer`: the
  agent is not interrupted; it reads the message at its next turn boundary.

Header actions: **Stop run** (confirm dialog; run ends `failed` / `run cancelled`),
**Resume run** on a finished run, **Refresh**, **Remove project** (records only; files stay).

The page polls every 2 s; there is no streaming.

## 5. Resume and rerun

- **Resume run** starts a new run of the *currently saved* profile. Workers that completed
  and whose model, prompt, effort and dependencies are unchanged are carried over (output,
  logs, status, marked *reused*); only failed, edited and unstarted workers execute again.
- **Rerun from here** (in a worker's panel) carries over everything except that worker and
  its dependents.
- If the saved profile differs from what the finished run used, a **Saved profile changed**
  banner lists the differences; if it is a different profile altogether, the canvas shows the
  saved profile's preview instead of the old run.

## 6. Connections (`/connections`)

An address book of machines: name, OS, IP, optional credential. Agents reach it through
host tools (`read_connections`, `claim_connection`, `release_connection`,
`reveal_connection`). A claimed connection shows *in use* with the owning agent and cannot be
edited or deleted until released. Credentials are stored in plaintext locally; **Reveal** on
the page shows them.

## 7. Costs (`/costs`)

Model spend per project and per model as reported by `omp` (tokens and USD). Unpriced
requests make a total *partial*; `—` means unknown, not free.

## Writing prompts that work with the host

- The root cannot do work itself; it only starts workers, reads outputs, answers questions
  and finishes. Put the actual task in worker prompts.
- Worker prompts can say "ask the root before doing X" — the worker already has
  `send_agent_message` and knows how to wait for the reply.
- Dependency outputs are passed to a worker as data (`dependency_outputs`), so tell a
  downstream worker what to look for in them rather than repeating the upstream task.
- Workers have no shell: anything that needs `npm`, `python` or a build must be done by
  you, or described as output for you to run.
