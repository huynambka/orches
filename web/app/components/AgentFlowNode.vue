<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import type { AgentStatus } from '~/lib/types';

defineProps<{ data: { role: string; status?: AgentStatus; label?: string } }>();
</script>

<template>
  <Handle type="target" :position="Position.Left" :connectable="false" />
  <strong>{{ data.label || data.role.replace(/^node_/, 'Agent ') }}</strong>
  <StatusBadge v-if="data.status" :status="data.status" />
  <Handle type="source" :position="Position.Right" :connectable="false" />
</template>
