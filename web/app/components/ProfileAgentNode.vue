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
    <NodeRoleIcon role="agent" />
    <span class="sr-only">{{ data.label }}</span>
  </div>
</template>
