<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';

defineProps<{ data: { label: string }; connectable?: boolean }>();

// One handle per side; with ConnectionMode.Loose any of them can start or end a
// connection, and edges pick the pair facing the other node dynamically.
const sides = [
  { id: 'left', position: Position.Left },
  { id: 'top', position: Position.Top },
  { id: 'right', position: Position.Right },
  { id: 'bottom', position: Position.Bottom },
] as const;
</script>

<template>
  <div class="profile-node">
    <Handle
      v-for="side in sides"
      :id="side.id"
      :key="side.id"
      type="source"
      :position="side.position"
      :connectable="connectable ?? false"
    />
    <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c1.4-3.2 4-5 7-5s5.6 1.8 7 5" /></svg></span>
    <span class="profile-node-label">{{ data.label }}</span>
  </div>
</template>
