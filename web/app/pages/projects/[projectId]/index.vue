<script setup lang="ts">
import { cancelRun, deleteProject, getPipelineProfiles, getProject, getRun, resumeRun, startProjectRun, updateProject,
  getMcpServers, getProjectMcpServers, addProjectMcpServer, editProjectMcpServer, removeProjectMcpServer,
  refreshProjectMcpServer, checkProjectMcpServer } from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import McpServerEditor from '~/components/McpServerEditor.vue';
import { hasDrift, profileDrift } from '~/lib/profileDiff';
import type { AgentId, McpCatalogEntry, McpServerConfig, PipelineProfile, ProjectChanges, ProjectDetail, ProjectMcpServer, RunDetail } from '~/lib/types';

const initialDraft = (project: ProjectDetail): ProjectChanges => ({
  name: project.name, target: project.target, initial_prompt: project.initial_prompt,
  project_directory: project.project_directory,
  pipeline_profile: project.pipeline_profile,
});

const projectId = useRoute().params.projectId as string;
const project = ref<ProjectDetail | null>(null);
const run = ref<RunDetail | null>(null);
const draft = ref<ProjectChanges | null>(null);
const dirty = ref(false);
const profiles = ref<PipelineProfile[]>([]);
const error = ref('');
const actionError = ref('');
const loading = ref(true);
const saving = ref(false);
const starting = ref(false);
const deleting = ref(false);
const stopping = ref(false);
const resuming = ref(false);

/* ------------------------------------------------------------ MCP state */
const mcpServers = ref<ProjectMcpServer[]>([]);
const mcpError = ref('');
const mcpLoading = ref(false);
const fileRevision = ref('');
const mcpSaving = ref(false);
const mcpChecking = ref<string | null>(null);
const catalogServers = ref<McpCatalogEntry[]>([]);
const mcpEditingName = ref<string | null>(null);
const mcpDraftName = ref('');
const mcpDraftConfig = ref<McpServerConfig>({ command: '' });
const mcpFormError = ref('');
const mcpAddMode = ref<'none' | 'catalog' | 'custom'>('none');

const load = async () => {
  try {
    const [current, configured] = await Promise.all([getProject(projectId), getPipelineProfiles()]);
    run.value = current.current_run ? await getRun(current.current_run.run_id) : null;
    project.value = current;
    profiles.value = configured.profiles;
    if (!dirty.value) draft.value = initialDraft(current);
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load project';
  } finally {
    loading.value = false;
  }
};

let timer: number | undefined;
onMounted(() => {
  void load();
  void loadMcp();
  timer = window.setInterval(() => void load(), 2000);
});
onUnmounted(() => { if (timer !== undefined) window.clearInterval(timer); });

/* ------------------------------------------------------------ settings */
const change = (next: ProjectChanges) => {
  dirty.value = true;
  draft.value = { ...draft.value, ...next };
  actionError.value = '';
};

const save = async (event: Event) => {
  event.preventDefault();
  if (!draft.value || saving.value || starting.value) return;
  saving.value = true;
  actionError.value = '';
  try {
    const { project_directory: _ignored, ...changes } = draft.value;
    await updateProject(projectId, changes);
    dirty.value = false;
    await load();
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to save project';
  } finally {
    saving.value = false;
  }
};

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);

const remove = async () => {
  if (deleting.value || saving.value || starting.value) return;
  const accepted = await confirmDialog.value?.ask({
    title: 'Remove project',
    message: 'Remove this project from Orches? The project folder and its files will be left untouched.',
    confirmLabel: 'Remove project',
    danger: true,
  });
  if (!accepted) return;
  deleting.value = true;
  actionError.value = '';
  try {
    await deleteProject(projectId);
    navigateTo('/');
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to remove project';
    deleting.value = false;
  }
};

const selectedProfile = computed(() => profiles.value.find((profile) => profile.id === draft.value?.pipeline_profile));
const running = computed(() => run.value?.run.status === 'running');
const runDisabledReason = computed(() => !draft.value?.pipeline_profile ? 'Select a pipeline profile first.'
  : !selectedProfile.value ? 'Selected pipeline profile is unavailable; choose another profile.'
    : selectedProfile.value.steps.length === 0 ? 'Add at least one agent to this profile before starting.'
      : !draft.value?.target?.trim() ? 'Set an objective first.'
        : dirty.value ? 'Save changes before starting.' : running.value ? 'A run is already active.' : '');

/* ---------------------------------------------------------------- runs */
const start = async () => {
  if (starting.value || saving.value || dirty.value) return;
  starting.value = true;
  actionError.value = '';
  try {
    await startProjectRun(projectId);
    await load();
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to start the pipeline';
    void load();
  } finally {
    starting.value = false;
  }
};

const stopRun = async () => {
  const current = run.value;
  if (!current || current.run.status !== 'running' || stopping.value) return;
  const accepted = await confirmDialog.value?.ask({
    title: 'Stop run',
    message: 'Stop this pipeline run? Running agents are terminated and the run is marked failed. Files already written stay in the project folder.',
    confirmLabel: 'Stop run',
    danger: true,
  });
  if (!accepted) return;
  stopping.value = true;
  actionError.value = '';
  try {
    await cancelRun(current.run.run_id);
    await load();
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to stop the run';
  } finally {
    stopping.value = false;
  }
};

const canResume = computed(() => {
  const current = run.value;
  return Boolean(current && current.run.status !== 'running' && current.pipeline_profile);
});

/** Difference between what this run executed and what the project has saved now. */
const drift = computed(() => {
  const snapshot = run.value?.pipeline_profile;
  const saved = project.value?.pipeline_profile_definition;
  if (!snapshot || !saved) return null;
  const result = profileDrift(snapshot, saved);
  return hasDrift(result) ? result : null;
});
const driftSummary = computed(() => {
  const current = drift.value;
  if (!current) return [];
  const parts: string[] = [];
  if (current.differentProfile) parts.push(`profile is now “${project.value?.pipeline_profile_definition?.name}”`);
  if (current.root) parts.push(`orchestrator model ${current.root.from || 'default'} → ${current.root.to || 'default'}`);
  for (const step of current.changed) {
    parts.push(step.fields.includes('model') ? `${step.name}: ${step.from} → ${step.to}` : `${step.name}: ${step.fields.join(', ')} changed`);
  }
  if (current.added.length) parts.push(`${current.added.length} agent${current.added.length === 1 ? '' : 's'} added`);
  if (current.removed.length) parts.push(`${current.removed.length} agent${current.removed.length === 1 ? '' : 's'} removed`);
  return parts;
});

/** New run of the project's saved profile. `rerun` forces one agent (and its dependents) to execute again. */
const resume = async (rerun: string[] = []) => {
  const current = run.value;
  if (!current || !canResume.value || resuming.value) return;
  const edited = new Set(drift.value?.changed.map((step) => step.id) ?? []);
  const completed = current.agents.filter((agent) => agent.role !== 'root' && agent.status === 'completed' && !edited.has(agent.role)).map((agent) => agent.role);
  const target = rerun[0];
  const changeNote = drift.value
    ? drift.value.differentProfile
      ? ' The project now selects a different profile, so every agent runs with it.'
      : ` The saved profile changed since this run${edited.size ? `; ${edited.size} edited agent${edited.size === 1 ? '' : 's'} and their dependents run again with it` : ' and is used for this run'}.`
    : '';
  const accepted = await confirmDialog.value?.ask({
    title: target ? 'Rerun from this agent' : 'Resume run',
    message: target
      ? `Start a new run that reruns this agent and everything that depends on it. Other completed agents keep their previous output. The finished run is replaced.${changeNote}`
      : `Start a new run with the saved profile. ${completed.length} completed agent${completed.length === 1 ? '' : 's'} keep their output; failed, edited and unstarted agents run again. The finished run is replaced.${changeNote}`,
    confirmLabel: target ? 'Rerun from here' : 'Resume run',
  });
  if (!accepted) return;
  resuming.value = true;
  actionError.value = '';
  try {
    await resumeRun(current.run.run_id, rerun);
    await load();
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to resume the run';
  } finally {
    resuming.value = false;
  }
};

/* --------------------------------------------------------------- MCP */
const loadMcp = async () => {
  mcpLoading.value = true;
  try {
    const [mcpData, catalog] = await Promise.all([getProjectMcpServers(projectId), getMcpServers()]);
    mcpServers.value = mcpData.servers;
    fileRevision.value = mcpData.file_revision;
    catalogServers.value = catalog.servers;
    mcpError.value = '';
  } catch (caught) {
    mcpError.value = caught instanceof Error ? caught.message : 'Unable to load MCP servers';
  } finally {
    mcpLoading.value = false;
  }
};

const availableCatalog = computed(() =>
  catalogServers.value.filter((c) => !mcpServers.value.some((s) => s.name === c.name)),
);

const mcpResetForm = () => {
  mcpEditingName.value = null;
  mcpDraftName.value = '';
  mcpDraftConfig.value = { command: '' };
  mcpFormError.value = '';
  mcpAddMode.value = 'none';
};

const applyMcpResult = (result: { file_revision: string; servers: ProjectMcpServer[] }) => {
  mcpServers.value = result.servers;
  fileRevision.value = result.file_revision;
};

const mcpAddFromCatalog = async (name: string) => {
  mcpSaving.value = true;
  mcpFormError.value = '';
  try {
    applyMcpResult(await addProjectMcpServer(projectId, { source: 'catalog', name, if_revision: fileRevision.value }));
    mcpAddMode.value = 'none';
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : 'Unable to add server';
    if (msg.includes('409')) mcpError.value = 'Configuration changed externally. Reload MCP data.';
    else mcpFormError.value = msg;
  } finally {
    mcpSaving.value = false;
  }
};

const mcpAddCustom = async (event: Event) => {
  event.preventDefault();
  if (mcpSaving.value) return;
  const name = mcpDraftName.value.trim();
  if (!name) { mcpFormError.value = 'Name is required.'; return; }
  mcpSaving.value = true;
  mcpFormError.value = '';
  try {
    applyMcpResult(await addProjectMcpServer(projectId, { source: 'custom', name, config: mcpDraftConfig.value, if_revision: fileRevision.value }));
    mcpResetForm();
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : 'Unable to add server';
    if (msg.includes('409')) mcpError.value = 'Configuration changed externally. Reload MCP data.';
    else mcpFormError.value = msg;
  } finally {
    mcpSaving.value = false;
  }
};

const mcpStartEdit = (server: ProjectMcpServer) => {
  mcpEditingName.value = server.name;
  mcpDraftName.value = server.name;
  mcpDraftConfig.value = { ...server.config };
  mcpFormError.value = '';
  mcpAddMode.value = 'none';
};

const mcpSaveEdit = async (event: Event) => {
  event.preventDefault();
  if (mcpSaving.value || !mcpEditingName.value) return;
  mcpSaving.value = true;
  mcpFormError.value = '';
  try {
    applyMcpResult(await editProjectMcpServer(projectId, mcpEditingName.value, mcpDraftConfig.value, fileRevision.value));
    mcpResetForm();
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : 'Unable to save server';
    if (msg.includes('409')) mcpError.value = 'Configuration changed externally. Reload MCP data.';
    else mcpFormError.value = msg;
  } finally {
    mcpSaving.value = false;
  }
};

const mcpRemove = async (server: ProjectMcpServer) => {
  const accepted = await confirmDialog.value?.ask({
    title: 'Remove MCP server',
    message: `Remove "${server.name}" from this project? The catalog entry (if any) is not affected.`,
    confirmLabel: 'Remove',
    danger: true,
  });
  if (!accepted) return;
  mcpFormError.value = '';
  try {
    applyMcpResult(await removeProjectMcpServer(projectId, server.name, fileRevision.value));
    if (mcpEditingName.value === server.name) mcpResetForm();
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : 'Unable to remove server';
    if (msg.includes('409')) mcpError.value = 'Configuration changed externally. Reload MCP data.';
    else mcpFormError.value = msg;
  }
};

const mcpRefresh = async (server: ProjectMcpServer) => {
  mcpSaving.value = true;
  mcpFormError.value = '';
  try {
    applyMcpResult(await refreshProjectMcpServer(projectId, server.name, fileRevision.value));
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : 'Unable to refresh server';
    if (msg.includes('409')) mcpError.value = 'Configuration changed externally. Reload MCP data.';
    else mcpFormError.value = msg;
  } finally {
    mcpSaving.value = false;
  }
};

const mcpCheck = async (server: ProjectMcpServer) => {
  mcpChecking.value = server.name;
  try {
    const result = await checkProjectMcpServer(projectId, server.name);
    const idx = mcpServers.value.findIndex((s) => s.name === server.name);
    if (idx >= 0) {
      mcpServers.value[idx] = { ...mcpServers.value[idx], status: result.status, last_checked: result.checked_at };
    }
  } catch (caught) {
    mcpFormError.value = caught instanceof Error ? caught.message : 'Check failed';
  } finally {
    mcpChecking.value = null;
  }
};

const mcpCheckAll = async () => {
  for (const server of mcpServers.value) {
    if (server.config.enabled === false) continue;
    await mcpCheck(server);
  }
};

const mcpStatusClass = (status: string) => {
  if (status === 'connected') return 'mcp-status-connected';
  if (status === 'failed') return 'mcp-status-failed';
  if (status === 'timeout') return 'mcp-status-timeout';
  if (status === 'disabled') return 'mcp-status-disabled';
  return 'mcp-status-unchecked';
};

const mcpStatusLabel = (status: string) => {
  if (status === 'connected') return 'Connected';
  if (status === 'failed') return 'Failed';
  if (status === 'timeout') return 'Timeout';
  if (status === 'disabled') return 'Disabled';
  return 'Not checked';
};

/* ------------------------------------------------------------- preview */
const encode = (id: string) => encodeURIComponent(id);
const previewProfile = computed(() => project.value?.pipeline_profile_definition ?? null);
/**
 * A finished run of a different profile is history, not the pipeline this project
 * runs next: show the saved profile instead (its nodes are what layout saves accept).
 */
const profileReplaced = computed(() =>
  Boolean(run.value && run.value.run.status !== 'running' && drift.value?.differentProfile));

/* Preview details appear only for the clicked node; nothing is selected until then. */
const previewSelected = ref<AgentId | null>(null);
watch(() => previewProfile.value?.id, () => { previewSelected.value = null; });
const previewStep = computed(() =>
  previewSelected.value && previewSelected.value !== 'root'
    ? previewProfile.value?.steps.find((step) => step.id === previewSelected.value) ?? null
    : null);
const previewStepName = (id: string) => previewProfile.value?.steps.find((step) => step.id === id)?.name || id;
const previewDependencies = computed(() => {
  const step = previewStep.value;
  if (!step) return '';
  return step.depends_on.length ? step.depends_on.map(previewStepName).join(', ') : 'Root orchestrator';
});
</script>

<template>
  <main class="page-shell"><div class="page">
    <ConfirmDialog ref="confirmDialog" />
    <div class="page-path"><NuxtLink to="/">Projects</NuxtLink> / <b>{{ project?.name || projectId }}</b></div>
    <div class="detail-head">
      <div>
        <h1 class="detail-title">{{ project?.name || 'Project' }}</h1>
        <span class="detail-id mono">{{ run ? `Run ${run.run.run_id}` : project?.project_id || projectId }}</span>
      </div>
      <div class="project-head-actions">
        <StatusBadge v-if="run" :status="run.run.status" />
        <button
          v-if="running"
          class="control control-danger"
          type="button"
          :disabled="stopping"
          @click="() => void stopRun()"
        >{{ stopping ? 'Stopping…' : 'Stop run' }}</button>
        <button
          v-if="canResume"
          class="control control-primary"
          type="button"
          :disabled="resuming"
          title="New run with the saved profile; unchanged completed agents keep their output"
          @click="() => void resume()"
        >{{ resuming ? 'Resuming…' : 'Resume run' }}</button>
        <button class="control" :disabled="loading || deleting" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button>
        <button class="control" type="button" :disabled="loading || saving || starting || deleting" @click="() => void remove()">{{ deleting ? 'Removing…' : 'Remove project' }}</button>
      </div>
    </div>
    <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
    <div v-if="actionError" class="error-state" role="alert">{{ actionError }}</div>
    <div v-if="!project || !draft" class="panel empty-state"><strong>{{ loading ? 'Loading project…' : 'Project unavailable' }}</strong></div>
    <template v-else>
      <div v-if="run && !run.pipeline_profile" class="error-state" role="alert">No pipeline profile snapshot is available for this run.</div>
      <div v-if="run && driftSummary.length" class="preview-banner" role="status">
        <span>
          <strong>Saved profile changed</strong> — {{ driftSummary.join('; ') }}.
          {{ run.run.status === 'running' ? 'This run keeps the profile it started with; the next run uses the saved profile.' : profileReplaced ? 'The finished run is kept until the next run starts; Resume and Start run the saved profile shown below.' : 'Models shown below are what this run used. Resume uses the saved profile; edited agents run again.' }}
        </span>
        <NuxtLink v-if="project.pipeline_profile" class="control" :to="`/profiles?edit=${encode(project.pipeline_profile)}`">Open profile</NuxtLink>
      </div>
      <div class="project-layout">
        <div class="project-stage">
          <!-- Active or finished run: real execution data only. -->
          <AgentWorkspace
            v-if="run && run.pipeline_profile && !profileReplaced"
            :key="`${run.run.run_id}-${run.pipeline_profile.id}`"
            :project-id="project.project_id"
            :run-id="run.run.run_id"
            :detail="run"
            :layout="project.agent_layout"
            @refresh="load"
            @rerun="(role) => void resume([role])"
          />
          <!-- No run yet: a labeled preview of the selected profile, never an invented execution. -->
          <template v-else-if="!run || profileReplaced">
            <template v-if="previewProfile">
              <div class="preview-banner" role="status">
                <span><strong>Preview</strong> — {{ run ? 'this profile has not run in this project yet' : 'this project has not run yet' }}. Showing the selected profile “{{ previewProfile.name }}”.</span>
                <NuxtLink class="control" :to="`/profiles?edit=${encode(previewProfile.id)}`">Edit profile</NuxtLink>
              </div>
              <section class="agent-workspace" aria-label="Profile preview">
                <AgentGraph
                  :key="`preview-${previewProfile.id}`"
                  :project-id="project.project_id"
                  :steps="previewProfile.steps"
                  :layout="project.agent_layout"
                  :selected="previewSelected"
                  @select="(role) => { previewSelected = role; }"
                />
                <div class="workflow-panel">
                  <div v-if="!previewSelected" class="empty-state">
                    <strong>No node selected</strong>
                    <span>Select a node to view its saved configuration.</span>
                  </div>
                  <template v-else-if="previewSelected === 'root'">
                    <div class="workflow-panel-heading">
                      <h3>Root orchestrator</h3>
                    </div>
                    <div class="panel dock-details">
                      <div><strong>Model</strong><span>{{ previewProfile.root_model || 'Server default' }}</span></div>
                      <div><strong>Effort</strong><span>{{ previewProfile.root_thinking || 'Server default' }}</span></div>
                      <div><strong>Prompt</strong><span v-if="previewProfile.root_prompt" class="preview-prompt">{{ previewProfile.root_prompt }}</span><span v-else>—</span></div>
                    </div>
                  </template>
                  <template v-else-if="previewStep">
                    <div class="workflow-panel-heading">
                      <h3>{{ previewStep.name }}</h3>
                    </div>
                    <div class="panel dock-details">
                      <div><strong>Model</strong><span>{{ previewStep.model_selector || '—' }}</span></div>
                      <div><strong>Effort</strong><span>{{ previewStep.thinking }}</span></div>
                      <div><strong>Depends on</strong><span>{{ previewDependencies }}</span></div>
                      <div><strong>Prompt</strong><span v-if="previewStep.prompt" class="preview-prompt">{{ previewStep.prompt }}</span><span v-else>—</span></div>
                      <div><strong>Id</strong><span class="mono">{{ previewStep.id }}</span></div>
                    </div>
                  </template>
                </div>
              </section>
            </template>
            <div v-else class="panel empty-state">
              <strong>No pipeline profile selected</strong>
              <span>Select a profile in the setup panel to preview and run its agents.</span>
            </div>
          </template>
        </div>
        <aside class="panel project-config project-setup" aria-label="Project configuration">
          <div class="project-config-heading"><div><h2 class="page-heading">Project setup</h2><p class="inline-note">The root orchestrator runs the selected profile's agents in dependency order. Save settings before starting.</p></div></div>
          <div class="project-setup-summary">
            <span><strong>Status</strong><StatusBadge v-if="project.current_run" :status="project.status" /><template v-else>Not started</template></span>
            <span><strong>Updated</strong>{{ new Date(project.updated_at).toLocaleString() }}</span>
            <ProgressBar :progress="project.progress" :has-run="Boolean(project.current_run)" />
          </div>
          <form class="project-form" @submit="(event) => void save(event)">
            <label>Project name<input class="control" required :maxlength="160" :value="draft.name || ''" @input="(event) => change({ name: (event.target as HTMLInputElement).value })"></label>
            <label>Project folder<input class="control" readonly :value="project.project_directory"></label>
            <label>Objective<textarea class="control" :maxlength="2000" :rows="2" :value="draft.target || ''" placeholder="What this pipeline should accomplish in this folder" @input="(event) => change({ target: (event.target as HTMLTextAreaElement).value })" /></label>
            <label>Initial brief for agents<textarea class="control" :maxlength="10000" :rows="5" :value="draft.initial_prompt || ''" placeholder="Context and constraints; this is shared as structured data, not as instructions." @input="(event) => change({ initial_prompt: (event.target as HTMLTextAreaElement).value })" /></label>
            <label>Pipeline profile<select class="control" :value="draft.pipeline_profile || ''" @change="(event) => change({ pipeline_profile: (event.target as HTMLSelectElement).value || null })"><option value="">No profile selected</option><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }}{{ profile.steps.length === 0 ? ' (draft)' : '' }}</option></select></label>
            <p v-if="!profiles.length" class="inline-note">No profiles yet. Create one in the <NuxtLink to="/profiles">Profiles</NuxtLink> tab.</p>
            <div class="project-form-actions">
              <button class="control" type="submit" :disabled="saving || starting || !dirty">{{ saving ? 'Saving…' : 'Save settings' }}</button>
              <button class="control control-primary" type="button" :disabled="starting || saving || Boolean(runDisabledReason)" @click="() => void start()">{{ starting ? 'Starting…' : 'Start pipeline →' }}</button>
              <span v-if="runDisabledReason" class="inline-note" role="status">{{ runDisabledReason }}</span>
            </div>
          </form>
          <!-- MCP servers section -->
          <div class="mcp-section">
            <div class="project-config-heading"><div>
              <h2 class="page-heading">MCP servers</h2>
              <p class="inline-note">Project-local MCP server configuration. Changes are saved to the project's <code>.omp/mcp.json</code> immediately.</p>
            </div></div>
            <div v-if="mcpError" class="error-state" role="alert">
              <span>{{ mcpError }}</span>
              <button class="control" @click="() => { mcpError = ''; void loadMcp(); }">Reload</button>
            </div>
            <div v-if="mcpLoading && !mcpServers.length" class="empty-state"><strong>Loading MCP servers…</strong></div>
            <template v-else>
              <!-- Server list -->
              <div v-if="mcpServers.length" class="mcp-server-list">
                <div v-for="server in mcpServers" :key="server.name" class="mcp-server-item">
                  <div class="mcp-server-header">
                    <span class="mono mcp-server-name">{{ server.name }}</span>
                    <span class="mcp-source-badge">{{ server.source === 'catalog' ? 'Catalog' : 'Custom' }}</span>
                    <span :class="`mcp-status ${mcpStatusClass(server.status)}`" :aria-label="`Status: ${mcpStatusLabel(server.status)}`">
                      <span class="status-dot" aria-hidden="true" />
                      {{ mcpStatusLabel(server.status) }}
                    </span>
                    <span v-if="server.last_checked" class="mcp-timestamp">Last check: {{ new Date(server.last_checked).toLocaleString() }}</span>
                  </div>
                  <div v-if="server.source === 'catalog' && server.source_missing" class="inline-note mcp-marker">⚠ Catalog entry deleted</div>
                  <div v-if="server.update_available" class="inline-note mcp-marker">↑ Newer catalog version available</div>
                  <div v-if="server.modified_locally" class="inline-note mcp-marker">✎ Modified locally</div>
                  <div class="mcp-server-actions">
                    <button class="control" type="button" :disabled="running || mcpSaving" @click="mcpStartEdit(server)">Edit</button>
                    <button class="control" type="button" :disabled="running || mcpSaving" @click="() => void mcpRemove(server)">Remove</button>
                    <button class="control" type="button" :disabled="mcpChecking !== null" @click="() => void mcpCheck(server)">
                      {{ mcpChecking === server.name ? 'Checking…' : 'Check status' }}
                    </button>
                    <button
                      v-if="server.source === 'catalog' && (server.update_available || server.modified_locally) && !server.source_missing"
                      class="control"
                      type="button"
                      :disabled="running || mcpSaving"
                      @click="() => void mcpRefresh(server)"
                    >Fetch newest config</button>
                  </div>
                </div>
              </div>
              <div v-else class="empty-state"><strong>No MCP servers configured</strong><span>Add servers from the catalog or create custom entries.</span></div>

              <!-- Edit form -->
              <template v-if="mcpEditingName">
                <div class="mcp-form-section">
                  <h3 class="page-heading">Edit "{{ mcpEditingName }}"</h3>
                  <form class="project-form" @submit="(event) => void mcpSaveEdit(event)">
                    <McpServerEditor
                      v-model="mcpDraftConfig"
                      :name="mcpDraftName"
                      :editing="true"
                      :disabled="mcpSaving"
                      @update:name="mcpDraftName = $event"
                    />
                    <div class="project-form-actions">
                      <button class="control control-primary" type="submit" :disabled="mcpSaving">{{ mcpSaving ? 'Saving…' : 'Save changes' }}</button>
                      <button class="control" type="button" :disabled="mcpSaving" @click="mcpResetForm">Cancel</button>
                    </div>
                    <p v-if="mcpFormError" class="create-error" role="alert">{{ mcpFormError }}</p>
                  </form>
                </div>
              </template>

              <!-- Add actions -->
              <div v-if="!mcpEditingName" class="mcp-add-actions">
                <div class="mcp-add-buttons">
                  <button class="control" type="button" :disabled="running || mcpSaving || !availableCatalog.length" @click="mcpAddMode = mcpAddMode === 'catalog' ? 'none' : 'catalog'">
                    {{ mcpAddMode === 'catalog' ? 'Cancel' : 'Add from catalog' }}
                  </button>
                  <button class="control" type="button" :disabled="running || mcpSaving" @click="mcpAddMode = mcpAddMode === 'custom' ? 'none' : 'custom'; mcpResetForm()">
                    {{ mcpAddMode === 'custom' ? 'Cancel' : 'Add custom' }}
                  </button>
                  <button v-if="mcpServers.length" class="control" type="button" :disabled="mcpChecking !== null" @click="() => void mcpCheckAll()">
                    {{ mcpChecking ? 'Checking…' : 'Check all' }}
                  </button>
                </div>

                <!-- Catalog selector -->
                <div v-if="mcpAddMode === 'catalog'" class="mcp-form-section">
                  <h3 class="page-heading">Add from catalog</h3>
                  <div v-if="availableCatalog.length" class="mcp-catalog-list">
                    <div v-for="entry in availableCatalog" :key="entry.name" class="mcp-catalog-item">
                      <span class="mono">{{ entry.name }}</span>
                      <span class="mcp-source-badge">{{ entry.transport }}</span>
                      <button class="control" type="button" :disabled="mcpSaving" @click="() => void mcpAddFromCatalog(entry.name)">Add</button>
                    </div>
                  </div>
                  <p v-else class="inline-note">All catalog servers are already added. <NuxtLink to="/mcp-servers">Manage catalog</NuxtLink></p>
                  <p v-if="mcpFormError" class="create-error" role="alert">{{ mcpFormError }}</p>
                </div>

                <!-- Custom form -->
                <div v-if="mcpAddMode === 'custom'" class="mcp-form-section">
                  <h3 class="page-heading">Add custom server</h3>
                  <form class="project-form" @submit="(event) => void mcpAddCustom(event)">
                    <McpServerEditor
                      v-model="mcpDraftConfig"
                      :name="mcpDraftName"
                      :editing="false"
                      :disabled="mcpSaving"
                      @update:name="mcpDraftName = $event"
                    />
                    <div class="project-form-actions">
                      <button class="control control-primary" type="submit" :disabled="mcpSaving">{{ mcpSaving ? 'Adding…' : 'Add server' }}</button>
                    </div>
                    <p v-if="mcpFormError" class="create-error" role="alert">{{ mcpFormError }}</p>
                  </form>
                </div>
              </div>
            </template>
          </div>
        </aside>
      </div>
    </template>
  </div></main>
</template>

<style scoped>
.mcp-section { margin-top: 20px; border-top: 1px solid var(--border); padding-top: 16px; }
.mcp-server-list { display: grid; gap: 12px; }
.mcp-server-item { padding: 10px 0; border-bottom: 1px solid var(--border); }
.mcp-server-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 14px; }
.mcp-server-name { font-weight: 600; }
.mcp-source-badge { font-size: 11px; padding: 1px 6px; border-radius: var(--radius-sm); background: var(--surface-2, var(--surface)); color: var(--muted); border: 1px solid var(--border); }
.mcp-status { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
.mcp-status-connected { color: var(--success); }
.mcp-status-failed { color: var(--error); }
.mcp-status-timeout { color: var(--warning); }
.mcp-status-disabled { color: var(--muted); }
.mcp-status-unchecked { color: var(--muted); }
.mcp-timestamp { font-size: 11px; color: var(--muted); }
.mcp-marker { font-size: 12px; margin-top: 2px; }
.mcp-server-actions { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
.mcp-add-actions { margin-top: 12px; }
.mcp-add-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
.mcp-form-section { margin-top: 12px; }
.mcp-catalog-list { display: grid; gap: 6px; margin-top: 8px; }
.mcp-catalog-item { display: flex; align-items: center; gap: 8px; font-size: 14px; }
</style>
