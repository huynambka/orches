<script setup lang="ts">
import { getCosts, getProjects } from '~/lib/api';
import { formatTokens, formatUsd, grandTotal, modelTotals, projectTotals, type ProjectTotal } from '~/lib/costs';
import type { CostRow, CostUsage, ProjectSummary } from '~/lib/types';

const rows = ref<CostRow[]>([]);
const projects = ref<ProjectSummary[]>([]);
const error = ref('');
const loading = ref(true);
const expanded = ref(new Set<string>());

const load = async () => {
  loading.value = true;
  try {
    const [costs, listing] = await Promise.all([getCosts(), getProjects()]);
    rows.value = costs.rows;
    projects.value = listing.projects;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load costs';
  } finally {
    loading.value = false;
  }
};
onMounted(() => { void load(); });

const byProject = computed(() => projectTotals(rows.value));
/** Every known project, with usage when recorded; projects deleted since keep their rows out. */
const projectRows = computed(() => {
  const totals = new Map(byProject.value.map((total) => [total.project_id, total]));
  const listed: Array<{ project_id: string; project_name: string; total: ProjectTotal | null }> = projects.value.map((project) => ({
    project_id: project.project_id, project_name: project.name, total: totals.get(project.project_id) ?? null,
  }));
  const listedIds = new Set(listed.map((item) => item.project_id));
  for (const total of byProject.value) {
    if (!listedIds.has(total.project_id)) listed.push({ project_id: total.project_id, project_name: total.project_name, total });
  }
  return listed;
});
const byModel = computed(() => modelTotals(rows.value));
const overall = computed(() => grandTotal(rows.value));

const toggle = (projectId: string) => {
  const next = new Set(expanded.value);
  if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
  expanded.value = next;
};

/** Cost cell: known total, flagged partial when some requests carried no price. */
const costLabel = (usage: CostUsage) => {
  const amount = formatUsd(usage.cost_usd);
  return usage.unpriced_requests > 0 && usage.cost_usd !== null ? `${amount} (partial)` : amount;
};
const unpricedLabel = (usage: CostUsage) => (usage.unpriced_requests > 0 ? `${usage.unpriced_requests} unpriced` : '');
</script>

<template>
  <main class="page-shell page-shell-narrow"><div class="page">
    <div class="detail-head">
      <div><h1 class="detail-title">Costs</h1><span class="detail-id">Model spend recorded from agent runs</span></div>
      <div class="project-head-actions"><button class="control" :disabled="loading" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button></div>
    </div>
    <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
    <section class="panel" aria-label="Cost summary">
      <div class="detail-summary">
        <span><strong>Known total</strong>{{ costLabel(overall) }}</span>
        <span><strong>Requests</strong>{{ overall.requests }}<template v-if="overall.unpriced_requests"> · {{ unpricedLabel(overall) }}</template></span>
        <span><strong>Input tokens</strong>{{ formatTokens(overall.input_tokens) }}</span>
        <span><strong>Output tokens</strong>{{ formatTokens(overall.output_tokens) }}</span>
      </div>
      <p class="inline-note cost-caption">Tracked OMP assistant and cache-warming requests, recorded since cost tracking was enabled. USD is OMP-estimated or server-reported, not a provider invoice; unpriced or internal requests not emitted as RPC events can make totals incomplete.</p>
    </section>
    <section class="panel" aria-label="Cost by project">
      <div class="panel-toolbar"><h2 class="page-heading">By project</h2><span class="toolbar-spacer" /><span class="toolbar-count">{{ projectRows.length }} projects</span></div>
      <div v-if="loading && !projectRows.length" class="empty-state"><strong>Loading costs…</strong></div>
      <div v-else-if="projectRows.length" class="tableWrap"><table>
        <thead><tr><th scope="col">Project</th><th scope="col">Cost (USD)</th><th scope="col">Requests</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Cache read</th><th scope="col">Cache write</th><th scope="col"><span class="sr-only">Models</span></th></tr></thead>
        <tbody>
          <template v-for="item in projectRows" :key="item.project_id">
            <tr>
              <td><NuxtLink class="row-title" :to="`/projects/${encodeURIComponent(item.project_id)}`">{{ item.project_name }}</NuxtLink></td>
              <template v-if="item.total">
                <td class="mono">{{ costLabel(item.total) }}</td>
                <td class="secondary-text">{{ item.total.requests }}<template v-if="item.total.unpriced_requests"> · {{ unpricedLabel(item.total) }}</template></td>
                <td class="secondary-text mono">{{ formatTokens(item.total.input_tokens) }}</td>
                <td class="secondary-text mono">{{ formatTokens(item.total.output_tokens) }}</td>
                <td class="secondary-text mono">{{ formatTokens(item.total.cache_read_tokens) }}</td>
                <td class="secondary-text mono">{{ formatTokens(item.total.cache_write_tokens) }}</td>
                <td><button class="control" type="button" :aria-expanded="expanded.has(item.project_id)" @click="() => toggle(item.project_id)">{{ expanded.has(item.project_id) ? 'Hide models' : `${item.total.models.length} models` }}</button></td>
              </template>
              <template v-else>
                <td class="secondary-text" colspan="7">No usage yet</td>
              </template>
            </tr>
            <tr v-for="model in item.total && expanded.has(item.project_id) ? item.total.models : []" :key="`${item.project_id}:${model.model}`" class="cost-model-row">
              <td class="secondary-text mono cost-model-name">{{ model.model }}</td>
              <td class="mono">{{ costLabel(model) }}</td>
              <td class="secondary-text">{{ model.requests }}<template v-if="model.unpriced_requests"> · {{ unpricedLabel(model) }}</template></td>
              <td class="secondary-text mono">{{ formatTokens(model.input_tokens) }}</td>
              <td class="secondary-text mono">{{ formatTokens(model.output_tokens) }}</td>
              <td class="secondary-text mono">{{ formatTokens(model.cache_read_tokens) }}</td>
              <td class="secondary-text mono">{{ formatTokens(model.cache_write_tokens) }}</td>
              <td />
            </tr>
          </template>
        </tbody>
      </table></div>
      <div v-else class="empty-state"><strong>No projects</strong><span>Costs appear here once a project runs its pipeline.</span></div>
    </section>
    <section class="panel" aria-label="Cost by model">
      <div class="panel-toolbar"><h2 class="page-heading">By model</h2><span class="toolbar-spacer" /><span class="toolbar-count">{{ byModel.length }} models</span></div>
      <div v-if="byModel.length" class="tableWrap"><table>
        <thead><tr><th scope="col">Model</th><th scope="col">Cost (USD)</th><th scope="col">Requests</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Cache read</th><th scope="col">Cache write</th></tr></thead>
        <tbody>
          <tr v-for="model in byModel" :key="model.model">
            <td class="row-title mono">{{ model.model }}</td>
            <td class="mono">{{ costLabel(model) }}</td>
            <td class="secondary-text">{{ model.requests }}<template v-if="model.unpriced_requests"> · {{ unpricedLabel(model) }}</template></td>
            <td class="secondary-text mono">{{ formatTokens(model.input_tokens) }}</td>
            <td class="secondary-text mono">{{ formatTokens(model.output_tokens) }}</td>
            <td class="secondary-text mono">{{ formatTokens(model.cache_read_tokens) }}</td>
            <td class="secondary-text mono">{{ formatTokens(model.cache_write_tokens) }}</td>
          </tr>
        </tbody>
      </table></div>
      <div v-else class="empty-state"><strong>No model usage recorded</strong><span>Totals accumulate as agents complete model requests.</span></div>
    </section>
  </div></main>
</template>
