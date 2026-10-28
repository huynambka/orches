<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';

defineProps<{ data: { label: string }; connectable?: boolean }>();
</script>

<template>
  <div class="profile-node">
    <Handle id="in" type="target" :position="Position.Left" :connectable="connectable ?? false" />
    <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c1.4-3.2 4-5 7-5s5.6 1.8 7 5" /></svg></span>
    <span class="profile-node-label">{{ data.label }}</span>
    <Handle id="out" type="source" :position="Position.Right" :connectable="connectable ?? false" />
  </div>
</template>
