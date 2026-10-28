# Orches — UI/UX Theme & Workflow Editor Guide

## Purpose

This guide defines the visual design and interaction standards for the **Orches** frontend.
It is adapted to the real application: do not reintroduce technologies, routes, or features
this document does not list.

Design references (visual/interaction inspiration only — their code, frameworks and
dependencies are **not** used here):

- **Studio Admin** (dashboard shell and management surfaces): https://studio-admin.arhamkhnz.com/
- **React Flow AI Workflow Editor** (workflow canvas layout and editing feel): https://reactflow.dev/ui/templates/ai-workflow-editor

The implementation library for all graph canvases is **Vue Flow** (https://vueflow.dev/guide/),
which is already installed and used.

---

## 1. Product Context

Orches is a local workspace that designs and runs user-defined AI agent pipelines against
local project folders.

Real, supported feature areas:

- **Projects** — a named binding to an existing local folder, with objective, initial brief
  and a selected pipeline profile; one current run at a time.
- **Profiles** — reusable agent pipeline definitions: a DAG of agent nodes
  (`name`, `model_selector`, `prompt`, `depends_on`, `position`) plus optional
  `root_prompt` / `root_model` for the fixed root orchestrator (model falls back to the
  server's `[roles.root]` default). Nothing else is configurable per node.
- **Runs** — a root orchestrator plus one invocation per profile node; live status,
  stdout conversation, stderr diagnostics, and guidance chat to running agents.
- **Connections** — an informational address book (name, OS, IP, optional credential).
  Never executable infrastructure.

There is no authentication, no multi-workspace, no run cancellation, no execution history,
no streaming API (polling only), and no node types beyond agent nodes. The UI must never
present controls for unsupported behavior or fabricate execution state.

## 2. Technology Stack (fixed)

- Nuxt 4 + Vue 3 + TypeScript (SSR, file-based routes)
- Plain global CSS with semantic custom properties (no Tailwind, no shadcn/ui, no Radix)
- `@vue-flow/core` with `@vue-flow/background`, `@vue-flow/controls`, `@vue-flow/minimap`
- Vitest for unit tests; ESLint; `vue-tsc` typecheck
- API access exclusively through `web/app/lib/api.ts` against the local FastAPI backend

Do not add React, Next.js, @xyflow/react, Zustand, TanStack, or any second UI library.

## 3. Routes (authoritative)

| Route | Purpose |
| --- | --- |
| `/` | Project list: search, filter, sort, paginate, create |
| `/projects/:projectId` | Project summary, settings, run start |
| `/projects/:projectId/agents` | Run workspace: pipeline graph, logs, chat |
| `/profiles` | Profile list; `?edit=<profile_id>` opens the editor workbench |
| `/connections` | Connections address book |

Navigation shows exactly Projects, Profiles, Connections. No Overview, Settings,
Executions, or Documentation entries.

## 4. Global Visual Design System

Minimal, elegant developer-tool interface: information hierarchy, density, readability.
Avoid gradients, oversized cards, heavy shadows, neon decoration, gratuitous animation.

### Themes

Both light and dark themes are implemented with semantic CSS variables on
`:root`/`[data-theme="dark"]`; components use only tokens, never hardcoded colors.

**Light (default)**

- Main background `#F8FAFC`; sidebar/card `#FFFFFF`; secondary surface `#F1F5F9`
- Text `#0F172A`; secondary text `#64748B`; border `#E2E8F0`
- Accent `#4F46E5`; accent hover `#4338CA`

**Dark (developer console)**

- Main background `#0F1117`; sidebar `#151922`; card `#1C2230`; secondary surface `#252C3A`
- Text `#F1F5F9`; secondary text `#94A3B8`; border `#303747`
- Accent `#8B5CF6`; accent hover `#7C3AED`

Plus semantic `success` (green), `warning` (amber), `error` (red), focus ring, radii
(6–10px), and Vue Flow canvas/node/edge/minimap variables mapped to the same tokens.
Theme preference (`light`/`dark`/`system`) persists in `localStorage['orches-theme']`;
system mode follows `prefers-color-scheme` live.

### Typography

- Sans: Google Sans Flex (variable, loaded from Google Fonts) with Inter/system UI fallback.
- Mono: Google Sans Code (Google Fonts, same stylesheet link) with SFMono/Consolas
  fallback; used for project/run/invocation IDs, folders, logs, JSON, prompts previews.
- Consistent type scale; metadata smaller than primary content.

### Components

Compact controls, subtle borders, minimal shadows, clear hover/focus/selected states,
consistent buttons/inputs/selects/dialogs/tables/badges, visible keyboard focus.
Status badges always pair color with text.

## 5. Application Shell

- Collapsible left sidebar: brand, Projects/Profiles/Connections with icons and
  `aria-current`; expanded ≈248px, collapsed ≈68px with tooltips; preference persists in
  `localStorage['orches-sidebar-collapsed']`.
- Compact top bar (≈56px): sidebar toggle, route-derived breadcrumb
  (e.g. Projects / <project name>), theme selector. No avatar, account menu, global search
  or notifications — none are backed by real functionality.
- Mobile: navigation drawer (native `<dialog>`; Escape/backdrop/route change closes,
  focus returns to the toggle).
- The profile editor (`/profiles?edit=<id>`) uses a distraction-free layout with compact
  chrome instead of the full dashboard sidebar.

## 6. Management Pages

### Projects (`/`)

- Header: title, short description, **New project** action.
- Toolbar: search (name, project ID, folder), status filter, All/Active tabs,
  updated/created sort, refresh.
- Data table (desktop) with name, folder, status badge, progress, current run, updated,
  actions; stacked cards on small screens.
- Distinct states: loading, no projects, no matches, API error with retry. An API error is
  never rendered as “zero projects”.
- Creation in an accessible modal dialog: required name and existing absolute folder,
  optional objective/brief/profile. Server errors shown inline; submission behavior
  unchanged.

### Project detail (`/projects/:projectId`)

Summary (folder, status, progress, updated), settings form (name, objective, brief,
profile select) with draft/dirty handling, explicit save, start gating (profile with ≥1
agent, objective set, no active run, saved draft) and remove-project action (records only;
the folder is untouched).

### Profiles (`/profiles`)

List rows with name, agent count, draft marker, Edit/Delete. Create via dialog (no
`window.prompt`). Deleting a profile selected by a project stays a 409 server error shown
to the user.

### Connections (`/connections`)

Informational address book. Form (name, OS, IP, credential) + table. Credentials are
masked everywhere; reveal is an explicit per-row action backed by the dedicated reveal
endpoint, abortable, and hidden again on demand; clearing requires explicit confirmation.
Copy must describe an address book, never an executable lab.

## 7. Profile Editor Workbench (`/profiles?edit=<id>`)

Inspired by the React Flow AI Workflow Editor layout, built with Vue Flow:

- **Top toolbar**: Back, profile name, dirty/saving/saved/error status, Undo, Redo,
  Auto-layout, Fit view, Validate, Save.
- **Left palette**: a single **Agent** node type — click to add, or drag onto the canvas
  (drop position projected with Vue Flow `project`). No other node categories exist.
- **Center canvas**: dotted background, zoom/pan, Controls, MiniMap, custom agent nodes +
  fixed root node, animated selection states. Every node has four edge handles
  (top/right/bottom/left, `ConnectionMode.Loose`); a dependency can be dragged between any
  two handles, and each edge anchors to whichever sides face the connected nodes
  (`anchorSides` in `lib/profileGraph.ts`: horizontal when |dx| ≥ |dy|, else vertical),
  re-evaluated live while dragging. The run/preview graph (`AgentGraph`) uses the same
  dynamic anchors.
- **Right inspector (~340px)**: selected agent name, model (from `GET /api/omp-models`,
  with previously saved but unavailable selectors kept visible as unavailable), effort
  (`THINKING_LEVELS` in `lib/types.ts`, default `medium`), prompt;
  root selection edits the orchestrator model, effort (server default or a level) and the
  supplemental root prompt; edge selection offers dependency
  removal. New agents default to the first available model; agents added while the model
  list is still loading are backfilled once it resolves, and Save runs the client-side
  draft validation first so a missing model blocks with a listed problem instead of a
  backend 400.

Rules preserved from the backend contract:

- Root is fixed, unconnectable and undeletable; root edges are derived automatically for
  nodes with no dependencies.
- Node IDs are stable `node_<32hex>`; max 64 nodes; names/models nonempty; effort is one of
  the omp thinking levels; prompts ≤10,000 chars; positions finite within ±10,000; no
  self/duplicate/cycle dependencies — rejected connections show the reason.
- Explicit save only (`PUT` with revision). A 409 conflict keeps the local draft and
  offers reload/discard; it never overwrites silently. Local undo/redo (bounded history)
  covers structural and text edits; dirty navigation warns.

Edges render at 2px (3px + accent when selected), with arrowheads pointing from prerequisite
to dependent in both the editor and run graph. Drag an agent handle onto another agent's
handle to create a directed dependency. Drag either endpoint of an existing dependency
edge to reconnect it; a valid drop is one undoable edit. Invalid, duplicate, cyclic, or
empty-canvas drops preserve the original edge. Automatic root edges cannot be reconnected.
Every dependency edge carries mid-edge buttons — **+** inserts an agent into the connection,
**×** removes it (root edges have no ×: they are derived, not user-owned).
Keyboard: Ctrl/Cmd+S save (outside text fields), Delete removes graph selection when the
canvas has focus, Escape clears selection; edge selection also offers removal in the
inspector. Never fight native text-field editing.

## 8. Run Workspace (`/projects/:projectId/agents`)

- Without a run: a clearly labeled **preview** of the selected profile graph (or an empty
  state prompting profile selection), with a link to project settings. No fabricated
  status, logs or chat.
- With a run: graph from the immutable run profile snapshot — root plus profile nodes,
  live status classes (pending/running/completed/failed/blocked, color + text), animated
  edges while running, Controls + MiniMap, node drag persistence per project
  (override > profile position > deterministic grid).
- Details dock (collapsible) for the selected node with tabs:
  - **Conversation** — parsed stdout conversation. Agent replies render as Markdown
    (`AgentMarkdown.vue` → `lib/markdown.ts`, markdown-it with `html: false`, images
    disabled, unsafe `javascript:`/`data:` links dropped); streamed partial text re-renders
    on every page. JSON-object replies keep the `StructuredValue` view; prompts, tool
    input/results and raw JSONL stay verbatim preformatted text.
  - **Diagnostics** — raw stderr.
  - **Details** — invocation timestamps, model, return code, error, artifact references.
- Guidance composer for running agents only (`chat_available`), 4000-char limit.
- Status comes exclusively from polling the real API (2s). No cancel button, no execution
  history, no simulated timeline.

## 9. Motion, Responsiveness, Accessibility

- Subtle transitions for sidebar, drawers, node selection, status changes; respect
  `prefers-reduced-motion`.
- Desktop: multi-panel editor; tablet/mobile: stacked panels, drawer navigation, list
  selection fallback for graph nodes; no horizontal overflow.
- Keyboard accessible controls, visible focus, ARIA labels on icon buttons, dialogs with
  focus management, form errors announced, color never the sole status signal.
- Memoize/limit graph re-renders; editing one node must not rebuild the whole graph.

## 10. Acceptance Criteria

1. Dashboard surfaces follow the Studio Admin-inspired clean design on these tokens.
2. The profile editor is a polished full-height Vue Flow workbench (palette, canvas,
   inspector, toolbar).
3. One design system spans dashboard and canvas; light and dark both work and persist.
4. Sidebar collapses/expands; mobile drawer works by keyboard.
5. All existing pages remain functional against the unchanged backend API.
6. Profiles load/save without data loss (stable IDs, dependencies, positions, revision).
7. Run state shown always mirrors real backend data; loading/empty/error/success states
   are distinct.
8. No TypeScript, lint, test or build regressions.
