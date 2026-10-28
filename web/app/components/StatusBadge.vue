<script setup lang="ts">
const props = defineProps<{ status: string }>();
const label = computed(() => props.status.replaceAll('_', ' '));
</script>

<template>
  <span :class="`status-badge status-${props.status}`" :aria-label="`Status: ${label}`">
    <span class="status-dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>
