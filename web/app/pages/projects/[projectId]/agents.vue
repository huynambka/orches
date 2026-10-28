<script setup lang="ts">
import { cancelRun, getProject, getRun } from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import type { ProjectDetail, RunDetail } from '~/lib/types';

const route = useRoute();
const projectId = route.params.projectId as string;

const project = ref<ProjectDetail | null>(null);
const run = ref<RunDetail | null>(null);
const error = ref('');

const load = async () => {
  try {
    const current = await getProject(projectId);
    project.value = current;
    run.value = current.current_run ? await getRun(current.current_run.run_id) : null;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load project';
  }
};

let timer: number | undefined;
onMounted(() => {
  void load();
  timer = window.setInterval(() => void load(), 2000);
});
onUnmounted(() => { if (timer) window.clearInterval(timer); });

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const stopping = ref(false);
const stopError = ref('');

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
  stopError.value = '';
  try {
    await cancelRun(current.run.run_id);
    await load();
  } catch (caught) {
    stopError.value = caught instanceof Error ? caught.message : 'Unable to stop the run';
  } finally {
    stopping.value = false;
  }
};

const encode = (id: string) => encodeURIComponent(id);
const previewProfile = computed(() => project.value?.pipeline_profile_definition ?? null);
</script>

<template>
  <main class="page-shell">
    <div class="page">
      <ConfirmDialog ref="confirmDialog" />
      <div class="page-path">
        <NuxtLink to="/">Projects</NuxtLink> / <NuxtLink :to="`/projects/${encode(projectId)}`">{{ project?.name || projectId }}</NuxtLink> / <b>Manage agents</b>
      </div>
      <div class="detail-head">
        <div>
          <h1 class="detail-title">Manage agents</h1>
          <span class="detail-id mono">{{ run ? `Run ${run.run.run_id}` : project?.project_id || projectId }}</span>
        </div>
        <div class="project-head-actions">
          <StatusBadge v-if="run" :status="run.run.status" />
          <button
            v-if="run && run.run.status === 'running'"
            class="control control-danger"
            type="button"
            :disabled="stopping"
            @click="() => void stopRun()"
          >{{ stopping ? 'Stopping…' : 'Stop run' }}</button>
          <NuxtLink class="control" :to="`/projects/${encode(projectId)}`">Project settings</NuxtLink>
        </div>
      </div>
      <div v-if="stopError" class="error-state" role="alert">{{ stopError }}</div>
      <div v-if="error" class="error-state" role="alert">{{ error }}</div>
      <div v-if="run && !run.pipeline_profile" class="error-state" role="alert">No pipeline profile snapshot is available for this run.</div>

      <!-- Active or finished run: real execution data only. -->
      <AgentWorkspace
        v-if="project && run && run.pipeline_profile"
        :key="`${run.run.run_id}-${run.pipeline_profile.id}`"
        :project-id="project.project_id"
        :run-id="run.run.run_id"
        :detail="run"
        :layout="project.agent_layout"
        @refresh="load"
      />

      <!-- No run yet: a labeled preview of the selected profile, never an invented execution. -->
      <template v-else-if="project && !run">
        <div v-if="previewProfile" class="workflow-stage">
          <div class="preview-banner" role="status">
            <span><strong>Preview</strong> — this project has not run yet. Showing the selected profile “{{ previewProfile.name }}”.</span>
            <NuxtLink class="control" :to="`/projects/${encode(projectId)}`">Start from project settings</NuxtLink>
          </div>
          <AgentGraph
            :key="`preview-${previewProfile.id}`"
            :project-id="project.project_id"
            :steps="previewProfile.steps"
            :layout="project.agent_layout"
          />
        </div>
        <div v-else class="panel empty-state">
          <strong>No pipeline profile selected</strong>
          <span>Select a profile in <NuxtLink :to="`/projects/${encode(projectId)}`">project settings</NuxtLink> to preview and run its agents.</span>
        </div>
      </template>
    </div>
  </main>
</template>
