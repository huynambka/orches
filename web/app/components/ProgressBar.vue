<script setup lang="ts">
import type { ProgressSummary } from '~/lib/types';

const props = withDefaults(defineProps<{ progress: ProgressSummary; hasRun?: boolean }>(), {
  hasRun: true,
});

const percent = computed(() =>
  props.progress.total ? Math.round((props.progress.completed / props.progress.total) * 100) : 0,
);
</script>

<template>
  <span v-if="!props.hasRun" class="progress-not-started">Not started · select a profile and start the pipeline</span>
  <div v-else class="progress-wrap" title="Successful agent stages in the latest run. Failed or pending stages do not count.">
    <div
      class="progress-track"
      role="progressbar"
      aria-label="Successful agent stages in latest run"
      :aria-valuemin="0"
      :aria-valuemax="props.progress.total"
      :aria-valuenow="props.progress.completed"
    >
      <div class="progress-fill" :style="{ width: `${percent}%` }" />
    </div>
    <div class="progress-copy">
      <span>{{ props.progress.completed }}/{{ props.progress.total }} stages completed</span>
      <span>{{ props.progress.running ? `${props.progress.running} running` : `${percent}%` }}</span>
    </div>
  </div>
</template>
