<script setup lang="ts">
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Position } from '@vue-flow/core';

defineOptions({ inheritAttrs: false });

const props = defineProps<{
  id: string;
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
  sourcePosition: Position;
  targetPosition: Position;
  markerEnd?: string;
  /** Dependency edges can be removed; automatic root links cannot. */
  removable?: boolean;
}>();

const emit = defineEmits<{ insert: [string]; remove: [string] }>();

const path = computed(() => getBezierPath({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  sourcePosition: props.sourcePosition,
  targetX: props.targetX,
  targetY: props.targetY,
  targetPosition: props.targetPosition,
}));
</script>

<template>
  <BaseEdge :id="id" :path="path[0]" :marker-end="markerEnd" />
  <EdgeLabelRenderer>
    <div
      class="edge-buttons nodrag nopan"
      :style="{ transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)` }"
    >
      <button
        class="edge-insert-button"
        type="button"
        title="Insert agent here"
        aria-label="Insert a new agent on this connection"
        @click.stop="emit('insert', props.id)"
      >+</button>
      <button
        v-if="removable"
        class="edge-insert-button edge-remove-button"
        type="button"
        title="Remove connection"
        aria-label="Remove this connection"
        @click.stop="emit('remove', props.id)"
      >×</button>
    </div>
  </EdgeLabelRenderer>
</template>
