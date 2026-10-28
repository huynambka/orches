<script setup lang="ts">
// Fixed icon per node role: the canvas shows only this icon, details live in the inspector.
defineProps<{ role: 'root' | 'agent' }>();
</script>

<template>
  <span class="node-role-icon" aria-hidden="true">
    <svg v-if="role === 'root'" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="m7.5 11 9-4M7.5 13l9 4" /></svg>
    <svg v-else viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c1.4-3.2 4-5 7-5s5.6 1.8 7 5" /></svg>
  </span>
</template>
