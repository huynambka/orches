<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';

defineProps<{ data: { label: string } }>();

// Root links are host-generated; handles exist only as dynamic edge anchors.
const sides = [
  { id: 'left', position: Position.Left },
  { id: 'top', position: Position.Top },
  { id: 'right', position: Position.Right },
  { id: 'bottom', position: Position.Bottom },
] as const;
</script>

<template>
  <div class="profile-node profile-node-root">
    <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="m7.5 11 9-4M7.5 13l9 4" /></svg></span>
    <strong class="profile-node-label">{{ data.label }}</strong>
    <Handle
      v-for="side in sides"
      :id="side.id"
      :key="side.id"
      type="source"
      :position="side.position"
      :connectable="false"
    />
  </div>
</template>
