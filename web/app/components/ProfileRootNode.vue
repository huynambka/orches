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
    <NodeRoleIcon role="root" />
    <span class="sr-only">{{ data.label }}</span>
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
