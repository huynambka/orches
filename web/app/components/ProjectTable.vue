<script setup lang="ts">
import type { ProjectSummary } from '~/lib/types';

defineProps<{ projects: ProjectSummary[] }>();

const encode = (id: string) => encodeURIComponent(id);
const formatDate = (value: string) => new Date(value).toLocaleString();
</script>

<template>
  <div class="tableWrap project-table">
    <table>
      <thead>
        <tr>
          <th scope="col">Project</th>
          <th scope="col">Folder</th>
          <th scope="col">Status</th>
          <th scope="col">Agent progress</th>
          <th scope="col">Current run</th>
          <th scope="col">Updated</th>
          <th scope="col"><span class="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="project in projects" :key="project.project_id">
          <td><NuxtLink class="row-title" :to="`/projects/${encode(project.project_id)}`">{{ project.name }}</NuxtLink></td>
          <td class="secondary-text mono">{{ project.project_directory }}</td>
          <td>
            <StatusBadge v-if="project.current_run" :status="project.status" />
            <span v-else class="status-badge">Not started</span>
          </td>
          <td><ProgressBar :progress="project.progress" :has-run="Boolean(project.current_run)" /></td>
          <td>
            <NuxtLink v-if="project.current_run" class="mono" :to="`/projects/${encode(project.project_id)}`">{{ project.current_run.run_id.slice(0, 12) }}…</NuxtLink>
            <span v-else class="secondary-text">—</span>
          </td>
          <td class="secondary-text">{{ formatDate(project.updated_at) }}</td>
          <td><NuxtLink class="row-action" :to="`/projects/${encode(project.project_id)}`" :aria-label="`Open ${project.name}`">Open <span aria-hidden="true">→</span></NuxtLink></td>
        </tr>
      </tbody>
    </table>
    <div class="project-cards">
      <article v-for="project in projects" :key="project.project_id" class="project-card">
        <div class="project-card-head">
          <NuxtLink class="row-title" :to="`/projects/${encode(project.project_id)}`">{{ project.name }}</NuxtLink>
          <StatusBadge v-if="project.current_run" :status="project.status" />
          <span v-else class="status-badge">Not started</span>
        </div>
        <span class="secondary-text mono">{{ project.project_directory }}</span>
        <ProgressBar :progress="project.progress" :has-run="Boolean(project.current_run)" />
        <div class="project-card-head">
          <span class="secondary-text">{{ formatDate(project.updated_at) }}</span>
          <NuxtLink class="row-action" :to="`/projects/${encode(project.project_id)}`" :aria-label="`Open ${project.name}`">Open <span aria-hidden="true">→</span></NuxtLink>
        </div>
      </article>
    </div>
  </div>
</template>
