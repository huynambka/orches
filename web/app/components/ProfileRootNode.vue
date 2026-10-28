<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';

defineProps<{ data: { label: string } }>();
</script>

<template>
  <div class="profile-node profile-node-root">
    <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="m7.5 11 9-4M7.5 13l9 4" /></svg></span>
    <strong class="profile-node-label">{{ data.label }}</strong>
    <Handle id="out" type="source" :position="Position.Right" :connectable="false" />
  </div>
</template>
