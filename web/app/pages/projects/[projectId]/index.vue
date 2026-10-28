<script setup lang="ts">
import { cancelRun, deleteProject, getPipelineProfiles, getProject, startProjectRun, updateProject } from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import type { PipelineProfile, ProjectChanges, ProjectDetail } from '~/lib/types';

const initialDraft = (project: ProjectDetail): ProjectChanges => ({
  name: project.name, target: project.target, initial_prompt: project.initial_prompt,
  project_directory: project.project_directory,
  pipeline_profile: project.pipeline_profile,
});

const projectId = useRoute().params.projectId as string;
const project = ref<ProjectDetail | null>(null);
const draft = ref<ProjectChanges | null>(null);
const dirty = ref(false);
const profiles = ref<PipelineProfile[]>([]);
const error = ref('');
const actionError = ref('');
const loading = ref(true);
const saving = ref(false);
const starting = ref(false);
const deleting = ref(false);

const load = async () => {
  try {
    const [current, configured] = await Promise.all([getProject(projectId), getPipelineProfiles()]);
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
  timer = window.setInterval(() => void load(), 5000);
});
onUnmounted(() => { if (timer !== undefined) window.clearInterval(timer); });

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

const start = async () => {
  if (starting.value || saving.value || dirty.value) return;
  starting.value = true;
  actionError.value = '';
  try {
    await startProjectRun(projectId);
    navigateTo(`/projects/${encodeURIComponent(projectId)}/agents`);
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to start the pipeline';
    void load();
  } finally {
    starting.value = false;
  }
};

const stopping = ref(false);

const stopRun = async () => {
  const current = project.value?.current_run;
  if (!current || current.status !== 'running' || stopping.value) return;
  const accepted = await confirmDialog.value?.ask({
    title: 'Stop run',
    message: 'Stop the active pipeline run? Running agents are terminated and the run is marked failed.',
    confirmLabel: 'Stop run',
    danger: true,
  });
  if (!accepted) return;
  stopping.value = true;
  actionError.value = '';
  try {
    await cancelRun(current.run_id);
    await load();
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : 'Unable to stop the run';
  } finally {
    stopping.value = false;
  }
};

const selectedProfile = computed(() => profiles.value.find((profile) => profile.id === draft.value?.pipeline_profile));
const running = computed(() => project.value?.current_run?.status === 'running');
const runDisabledReason = computed(() => !draft.value?.pipeline_profile ? 'Select a pipeline profile first.'
  : !selectedProfile.value ? 'Selected pipeline profile is unavailable; choose another profile.'
    : selectedProfile.value.steps.length === 0 ? 'Add at least one agent to this profile before starting.'
      : !draft.value?.target?.trim() ? 'Set an objective first.'
        : dirty.value ? 'Save changes before starting.' : running.value ? 'A run is already active.' : '');
</script>

<template>
  <main class="page-shell"><div class="page">
    <ConfirmDialog ref="confirmDialog" />
    <div class="page-path"><NuxtLink to="/">Projects</NuxtLink> / <b>{{ project?.name || projectId }}</b></div>
    <div class="detail-head"><div><h1 class="detail-title">{{ project?.name || 'Project' }}</h1><span class="detail-id mono">{{ project?.project_id || projectId }}</span></div><div class="project-head-actions"><NuxtLink class="control" :to="`/projects/${encodeURIComponent(projectId)}/agents`">Manage agents</NuxtLink><button class="control" :disabled="loading || deleting" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button><button class="control" type="button" :disabled="loading || saving || starting || deleting" @click="() => void remove()">{{ deleting ? 'Removing…' : 'Remove project' }}</button></div></div>
    <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
    <div v-if="actionError" class="error-state" role="alert">{{ actionError }}</div>
    <div v-if="!project || !draft" class="panel empty-state"><strong>{{ loading ? 'Loading project…' : 'Project unavailable' }}</strong></div>
    <template v-else>
      <section class="panel" aria-label="Project summary"><div class="detail-summary"><span><strong>Folder</strong>{{ project.project_directory }}</span><span><strong>Status</strong><StatusBadge v-if="project.current_run" :status="project.status"/><template v-else>Not started</template></span><span><strong>Updated</strong>{{ new Date(project.updated_at).toLocaleString() }}</span><ProgressBar :progress="project.progress" :has-run="Boolean(project.current_run)"/></div></section>
      <section class="panel project-config" aria-label="Project configuration">
        <div class="project-config-heading"><div><h2 class="page-heading">Project setup</h2><p class="inline-note">The root orchestrator runs the selected profile's agents in dependency order. Save settings before starting.</p></div></div>
        <form class="project-form" @submit="(event) => void save(event)">
          <label>Project name<input class="control" required :maxlength="160" :value="draft.name || ''" @input="(event) => change({ name: (event.target as HTMLInputElement).value })"/></label>
          <label>Project folder<input class="control" readonly :value="project.project_directory" /></label>
          <label>Objective<textarea class="control" :maxlength="2000" :rows="2" :value="draft.target || ''" @input="(event) => change({ target: (event.target as HTMLTextAreaElement).value })" placeholder="What this pipeline should accomplish in this folder"/></label>
          <label>Initial brief for agents<textarea class="control" :maxlength="10000" :rows="4" :value="draft.initial_prompt || ''" @input="(event) => change({ initial_prompt: (event.target as HTMLTextAreaElement).value })" placeholder="Context and constraints; this is shared as structured data, not as instructions."/></label>
          <div class="project-form-row">
            <label>Pipeline profile<select class="control" :value="draft.pipeline_profile || ''" @change="(event) => change({ pipeline_profile: (event.target as HTMLSelectElement).value || null })"><option value="">No profile selected</option><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }}{{ profile.steps.length === 0 ? ' (draft)' : '' }}</option></select></label>
          </div>
          <p v-if="!profiles.length" class="inline-note">No profiles yet. Create one in the <NuxtLink to="/profiles">Profiles</NuxtLink> tab.</p>
          <div class="project-form-actions"><button class="control" type="submit" :disabled="saving || starting || !dirty">{{ saving ? 'Saving…' : 'Save settings' }}</button><button class="control control-primary" type="button" :disabled="starting || saving || Boolean(runDisabledReason)" @click="() => void start()">{{ starting ? 'Starting…' : 'Start pipeline →' }}</button><button v-if="running" class="control control-danger" type="button" :disabled="stopping" @click="() => void stopRun()">{{ stopping ? 'Stopping…' : 'Stop run' }}</button><span v-if="runDisabledReason" class="inline-note" role="status">{{ runDisabledReason }}</span></div>
        </form>
      </section>
    </template>
  </div></main>
</template>
