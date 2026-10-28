<script setup lang="ts">
import { VueFlow, type Edge, type Node } from '@vue-flow/core';
import { Background } from '@vue-flow/background';
import { Controls } from '@vue-flow/controls';
import { MiniMap } from '@vue-flow/minimap';
import { saveAgentPosition } from '~/lib/api';
import type { AgentId, AgentLayout, AgentStatus, PipelineStep } from '~/lib/types';

const props = withDefaults(defineProps<{
  projectId: string;
  steps: PipelineStep[];
  layout: AgentLayout;
  /** Live statuses by role; omit entirely for a profile preview with no execution state. */
  statuses?: Partial<Record<AgentId, AgentStatus>>;
  animated?: boolean;
  selected?: AgentId | null;
}>(), {
  statuses: undefined,
  animated: false,
  selected: null,
});
const emit = defineEmits<{ select: [AgentId] }>();

const nameById = computed(() => {
  const result: Record<string, string> = { root: 'Orchestrator' };
  for (const step of props.steps) result[step.id] = step.name;
  return result;
});
const labelFor = (id: string) => nameById.value[id] || id.replace(/^node_/, 'Agent ');
const clamp = (value: number) => Math.min(10000, Math.max(-10000, Math.round(value)));

const statusFor = (role: AgentId): AgentStatus | undefined => props.statuses?.[role] ?? (props.statuses ? 'pending' : undefined);
const nodeClass = (role: AgentId) => {
  const status = statusFor(role);
  return `workflow-node${status ? ` workflow-${status}` : ''}${props.selected === role ? ' workflow-selected' : ''}`;
};

// Saved project overrides win, then the profile's own node position, then a grid.
const positionFor = (role: AgentId, index: number) => {
  const override = props.layout[role];
  if (override) return override;
  if (role === 'root') return { x: 30, y: 110 };
  const step = props.steps.find((item) => item.id === role);
  if (step?.position) return step.position;
  return { x: 300 + (index % 4) * 260, y: 80 + Math.floor(index / 4) * 150 };
};

const makeNode = (role: AgentId, index: number): Node => ({
  id: role,
  type: 'agent',
  position: positionFor(role, index),
  data: { role, status: statusFor(role), label: labelFor(role) },
  class: nodeClass(role),
  style: { width: '210px' },
});

const visibleRoles = computed<AgentId[]>(() => ['root', ...props.steps.map((step) => step.id)]);
const nodes = ref<Node[]>(visibleRoles.value.map(makeNode));

const edges = computed<Edge[]>(() => {
  const result: Edge[] = [];
  for (const step of props.steps.filter((item) => item.depends_on.length === 0)) {
    result.push({ id: `root-${step.id}`, source: 'root', target: step.id, animated: props.animated });
  }
  for (const step of props.steps) {
    for (const dependency of step.depends_on) {
      result.push({ id: `${dependency}-${step.id}`, source: dependency, target: step.id, animated: props.animated });
    }
  }
  return result;
});

const savingRole = ref<AgentId | null>(null);
const layoutMessage = ref('');
const dragging = ref(false);
const dragOrigin: Record<string, { x: number; y: number }> = {};
const nodesDraggable = computed(() => savingRole.value === null);

// Refresh node status/selection in place without resetting drag positions.
watch([() => props.statuses, () => props.selected, () => props.steps], () => {
  if (dragging.value) return;
  const known = new Set(nodes.value.map((node) => node.id));
  const next: Node[] = nodes.value
    .filter((node) => visibleRoles.value.includes(node.id))
    .map((node) => {
      const role = (node.data as { role: AgentId }).role;
      return { ...node, data: { role, status: statusFor(role), label: labelFor(role) }, class: nodeClass(role) };
    });
  visibleRoles.value.forEach((role, index) => {
    if (!known.has(role)) next.push(makeNode(role, Math.max(0, index - 1)));
  });
  nodes.value = next;
}, { deep: true });

const onNodeClick = ({ node }: { node: Node }) => emit('select', node.id);

const onNodeDragStart = ({ node }: { node: Node }) => {
  dragging.value = true;
  dragOrigin[node.id] = { ...node.position };
};

const onNodeDragStop = ({ node }: { node: Node }) => {
  dragging.value = false;
  const origin = dragOrigin[node.id];
  delete dragOrigin[node.id];
  const target = { x: clamp(node.position.x), y: clamp(node.position.y) };
  if (!origin || (origin.x === target.x && origin.y === target.y)) return;
  nodes.value = nodes.value.map((item) => (item.id === node.id ? { ...item, position: target } : item));
  savingRole.value = node.id;
  layoutMessage.value = 'Saving layout…';
  void saveAgentPosition(props.projectId, node.id, target)
    .then(() => { layoutMessage.value = `Saved position for ${labelFor(node.id)}`; })
    .catch((error: unknown) => {
      nodes.value = nodes.value.map((item) => (item.id === node.id ? { ...item, position: origin } : item));
      layoutMessage.value = error instanceof Error ? error.message : 'Unable to save layout';
    })
    .finally(() => { savingRole.value = null; });
};
</script>

<template>
  <div class="workflow-stage">
    <div class="workflow-canvas">
      <ClientOnly>
        <VueFlow
          v-model:nodes="nodes"
          :edges="edges"
          :nodes-connectable="false"
          :delete-key-code="null"
          :nodes-draggable="nodesDraggable"
          fit-view-on-init
          :min-zoom="0.35"
          @node-click="onNodeClick"
          @node-drag-start="onNodeDragStart"
          @node-drag-stop="onNodeDragStop"
        >
          <template #node-agent="agentProps">
            <AgentFlowNode :data="agentProps.data" />
          </template>
          <Background pattern-color="var(--canvas-dot)" :gap="18" />
          <Controls />
          <MiniMap pannable zoomable />
        </VueFlow>
      </ClientOnly>
    </div>
    <div class="workflow-stage-note">
      <span class="inline-note">Drag nodes to arrange; positions are saved per project.</span>
      <span v-if="layoutMessage" class="inline-note" role="status">{{ layoutMessage }}</span>
    </div>
  </div>
</template>
