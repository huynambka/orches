<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { AgentStatus } from '~/lib/types';

const props = defineProps<{ data: { role: string; status?: AgentStatus; label?: string } }>();

// Display-only anchors; edges pick the side facing the other node dynamically.
const sides = [
  { id: 'left', position: Position.Left },
  { id: 'top', position: Position.Top },
  { id: 'right', position: Position.Right },
  { id: 'bottom', position: Position.Bottom },
] as const;

const name = computed(() => props.data.label || props.data.role.replace(/^node_/, 'Agent '));
</script>

<template>
  <Handle
    v-for="side in sides"
    :id="side.id"
    :key="side.id"
    type="source"
    :position="side.position"
    :connectable="false"
  />
  <NodeRoleIcon :role="data.role === 'root' ? 'root' : 'agent'" />
  <span class="sr-only">{{ name }}{{ data.status ? ` · ${data.status}` : '' }}</span>
</template>
