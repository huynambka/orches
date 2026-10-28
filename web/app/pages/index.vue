<script setup lang="ts">
import { createProject, getPipelineProfiles, getProjects } from '~/lib/api';
import type { PipelineProfile, ProjectSummary } from '~/lib/types';

const submittingRef = { current: false };
const projects = ref<ProjectSummary[]>([]);
const error = ref('');
const loading = ref(true);
const search = ref('');
const status = ref('all');
const tab = ref<'all' | 'active'>('all');
const sortBy = ref<'updated' | 'created'>('updated');
const pageSize = ref(20);
const page = ref(0);

const createDialog = ref<HTMLDialogElement | null>(null);
const showCreate = ref(false);
const profiles = ref<PipelineProfile[]>([]);
const selectedProfile = ref('');
const name = ref('');
const target = ref('');
const initialPrompt = ref('');
const projectDirectory = ref('');
const createError = ref('');
const submitting = ref(false);

const load = async () => {
  try {
    projects.value = (await getProjects()).projects;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load projects';
  } finally {
    loading.value = false;
  }
};

const loadProfiles = () => {
  void getPipelineProfiles().then((result) => {
    profiles.value = result.profiles;
    if (!result.profiles.some((profile) => profile.id === selectedProfile.value)) {
      selectedProfile.value = '';
    }
  }).catch(() => { profiles.value = []; });
};

let timer: number | undefined;
onMounted(() => {
  void load();
  timer = window.setInterval(() => void load(), 5000);
  loadProfiles();
});
onUnmounted(() => { if (timer !== undefined) window.clearInterval(timer); });

const submitProject = async (event: Event) => {
  event.preventDefault();
  if (submittingRef.current) return;
  if (!name.value.trim()) {
    createError.value = 'Enter a project name.';
    return;
  }
  if (!projectDirectory.value) {
    createError.value = 'Choose a project folder.';
    return;
  }
  submittingRef.current = true;
  submitting.value = true;
  createError.value = '';
  try {
    const created = await createProject({
      name: name.value.trim(),
      project_directory: projectDirectory.value,
      pipeline_profile: selectedProfile.value || null,
      target: target.value,
      initial_prompt: initialPrompt.value,
    });
    projects.value = [created, ...projects.value.filter((project) => project.project_id !== created.project_id)];
    showCreate.value = false;
    name.value = '';
    target.value = '';
    initialPrompt.value = '';
    projectDirectory.value = '';
    navigateTo(`/projects/${encodeURIComponent(created.project_id)}`);
  } catch (caught) {
    createError.value = caught instanceof Error ? caught.message : 'Unable to create project';
  } finally {
    submittingRef.current = false;
    submitting.value = false;
  }
};

const filtered = computed(() => {
  const matches = projects.value.filter((project) => {
    const matchesSearch = `${project.name} ${project.project_id} ${project.project_directory}`
      .toLowerCase().includes(search.value.trim().toLowerCase());
    const visibleStatus = project.current_run ? project.status : 'not_started';
    const matchesStatus = status.value === 'all' || visibleStatus === status.value;
    const matchesTab = tab.value === 'all' || visibleStatus === 'active';
    return matchesSearch && matchesStatus && matchesTab;
  });
  const key = sortBy.value === 'updated' ? 'updated_at' : 'created_at';
  return [...matches].sort((a, b) => (a[key] < b[key] ? 1 : a[key] > b[key] ? -1 : 0));
});
const start = computed(() => page.value * pageSize.value);
const visible = computed(() => filtered.value.slice(start.value, start.value + pageSize.value));
const openCreate = () => {
  showCreate.value = true;
  createError.value = '';
  createDialog.value?.showModal();
};
const cancelCreate = () => {
  showCreate.value = false;
  createError.value = '';
  createDialog.value?.close();
};
</script>

<template>
  <main class="page-shell">
    <div class="page">
      <div class="page-tabs" role="tablist" aria-label="Project view">
        <button :class="`page-tab ${tab === 'all' ? 'active' : ''}`" role="tab" :aria-selected="tab === 'all'" @click="() => { tab = 'all'; page = 0; }">All projects</button>
        <button :class="`page-tab ${tab === 'active' ? 'active' : ''}`" role="tab" :aria-selected="tab === 'active'" @click="() => { tab = 'active'; page = 0; }">Active</button>
      </div>
      <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
      <dialog ref="createDialog" class="modal" aria-labelledby="create-project-heading" @close="showCreate = false" @cancel="cancelCreate">
        <div class="modal-head">
          <div>
            <h2 id="create-project-heading" class="page-heading">New project</h2>
            <p class="inline-note">Pick a local folder and an agent pipeline profile; the objective can be set here or on the project page.</p>
          </div>
          <button class="icon-button" type="button" aria-label="Close dialog" :disabled="submitting" @click="cancelCreate"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg></button>
        </div>
        <form class="create-form modal-body" @submit="(event) => void submitProject(event)">
          <label class="create-field">Project name <input class="control" required :maxlength="160" v-model="name" placeholder="Release triage" /></label>
          <label class="create-field">Project folder <input class="control" required v-model="projectDirectory" placeholder="C:\path\to\existing\folder" /></label>
          <label class="create-field">Pipeline profile <select class="control" v-model="selectedProfile"><option value="">Choose later</option><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }}{{ profile.steps.length === 0 ? ' (draft)' : '' }}</option></select></label>
          <label class="create-field">Objective <input class="control" :maxlength="2000" v-model="target" placeholder="What should this pipeline accomplish?" /></label>
          <label class="create-field">Initial brief <textarea class="control" :maxlength="10000" :rows="3" v-model="initialPrompt" placeholder="Context and constraints shared with every agent" /></label>
          <p v-if="createError" id="create-project-error" class="create-error" role="alert">{{ createError }}</p>
          <div class="modal-actions">
            <button class="control" type="button" :disabled="submitting" @click="cancelCreate">Cancel</button>
            <button class="control control-primary" type="submit" :disabled="submitting">{{ submitting ? 'Creating…' : 'Create project' }}</button>
          </div>
        </form>
      </dialog>
      <section class="panel" aria-label="Projects">
        <div class="panel-toolbar">
          <label class="search-field"><span class="search-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/></svg></span><input class="control" type="search" placeholder="Search name, folder or ID" v-model="search" aria-label="Search projects" @input="() => { page = 0; }" /></label>
          <select class="control filter-select" aria-label="Filter by status" v-model="status" @change="() => { page = 0; }"><option value="all">All statuses</option><option value="not_started">Not started</option><option value="active">Active</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="blocked">Blocked</option></select>
          <select class="control filter-select" aria-label="Sort projects" v-model="sortBy"><option value="updated">Recently updated</option><option value="created">Recently created</option></select>
          <span class="toolbar-count">{{ filtered.length }} projects</span>
          <span class="toolbar-spacer" />
          <span class="inline-note">Design a profile · run it against a project folder</span>
          <button class="control control-primary" type="button" :aria-expanded="showCreate" @click="openCreate">New project</button>
          <button class="control" :disabled="loading" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button>
        </div>
        <div v-if="loading && !projects.length" class="empty-state"><strong>Loading projects…</strong></div>
        <ProjectTable v-else-if="filtered.length" :projects="visible" />
        <div v-else class="empty-state"><strong>{{ search || status !== 'all' || tab !== 'all' ? 'No matching projects' : 'No projects yet' }}</strong><span>{{ search || status !== 'all' || tab !== 'all' ? 'Try another search or filter.' : 'Create a project, select a profile, then start the pipeline.' }}</span></div>
        <div class="table-footer"><select aria-label="Rows per page" :value="pageSize" @change="(event) => { pageSize = Number((event.target as HTMLSelectElement).value); page = 0; }"><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option></select><span>per page · {{ filtered.length ? `${start + 1}–${Math.min(start + pageSize, filtered.length)}` : '0' }} of {{ filtered.length }}</span><span class="toolbar-spacer" /><button class="control" :disabled="page === 0" @click="() => page = page - 1">Previous</button><button class="control" :disabled="start + pageSize >= filtered.length" @click="() => page = page + 1">Next</button></div>
      </section>
    </div>
  </main>
</template>
