<script setup lang="ts">
import { VueFlow, useVueFlow, ConnectionMode, type Connection, type Edge, type Node, type NodeChange, type EdgeChange } from '@vue-flow/core';
import { Background } from '@vue-flow/background';
import { Controls } from '@vue-flow/controls';
import { MiniMap } from '@vue-flow/minimap';
import { getOmpModels, getPipelineProfile, updatePipelineProfile, type OmpModel } from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import { connectSteps, connectionError, layoutProfileSteps, removeDependency, removeStep, validateProfileDraft } from '~/lib/profileGraph';
import type { PipelineProfile, PipelineStep } from '~/lib/types';

const props = defineProps<{ profile: PipelineProfile }>();
const emit = defineEmits<{ saved: [PipelineProfile]; cancel: [] }>();

interface Snapshot { name: string; steps: PipelineStep[]; rootPrompt: string; rootModel: string }

const snapshotOf = (profile: PipelineProfile): Snapshot => ({
  name: profile.name, steps: cloneSteps(profile.steps),
  rootPrompt: profile.root_prompt || '', rootModel: profile.root_model || '',
});

const nodeId = () => `node_${crypto.randomUUID().replaceAll('-', '')}`;
const positionFor = (index: number) => ({ x: 80 + (index % 4) * 280, y: 80 + Math.floor(index / 4) * 150 });
const cloneSteps = (steps: PipelineStep[]): PipelineStep[] =>
  steps.map((step) => ({ ...step, depends_on: [...step.depends_on], position: { ...step.position } }));

const name = ref(props.profile.name);
const steps = ref<PipelineStep[]>(cloneSteps(props.profile.steps));
const rootPrompt = ref(props.profile.root_prompt || '');
const rootModel = ref(props.profile.root_model || '');
const revision = ref(props.profile.revision ?? 1);
const baseline = ref<Snapshot>(snapshotOf(props.profile));

const selectedNode = ref<string | null>(null);
const selectedEdgeId = ref<string | null>(null);
const models = ref<OmpModel[]>([]);
const modelError = ref('');
const saving = ref(false);
const saveError = ref('');
const savedAt = ref(0);
const conflict = ref(false);
const connectNotice = ref('');
const validation = ref<string[]>([]);
const editorRoot = ref<HTMLElement | null>(null);
const canvasEl = ref<HTMLElement | null>(null);

const snapshotEquals = (a: Snapshot, b: Snapshot) => JSON.stringify(a) === JSON.stringify(b);
const currentSnapshot = (): Snapshot => ({
  name: name.value, steps: cloneSteps(steps.value), rootPrompt: rootPrompt.value, rootModel: rootModel.value,
});
const dirty = computed(() => !snapshotEquals(currentSnapshot(), baseline.value));

/* ------------------------------------------------------------- history */
const MAX_HISTORY = 50;
const undoStack = ref<Snapshot[]>([]);
const redoStack = ref<Snapshot[]>([]);
let pendingTextSnapshot: Snapshot | null = null;

const pushHistory = (snapshot: Snapshot) => {
  undoStack.value = [...undoStack.value.slice(-(MAX_HISTORY - 1)), snapshot];
  redoStack.value = [];
};

/** Capture state before a structural mutation (add/remove/connect/layout). */
const commit = (mutate: () => void) => {
  const before = currentSnapshot();
  mutate();
  if (!snapshotEquals(before, currentSnapshot())) pushHistory(before);
};

const beginTextEdit = () => { pendingTextSnapshot = currentSnapshot(); };
const endTextEdit = () => {
  if (pendingTextSnapshot && !snapshotEquals(pendingTextSnapshot, currentSnapshot())) {
    pushHistory(pendingTextSnapshot);
  }
  pendingTextSnapshot = null;
};

const applySnapshot = (snapshot: Snapshot) => {
  name.value = snapshot.name;
  steps.value = cloneSteps(snapshot.steps);
  rootPrompt.value = snapshot.rootPrompt;
  rootModel.value = snapshot.rootModel;
  if (selectedNode.value && selectedNode.value !== 'root'
    && !snapshot.steps.some((step) => step.id === selectedNode.value)) selectedNode.value = null;
  selectedEdgeId.value = null;
};

const undo = () => {
  const previous = undoStack.value[undoStack.value.length - 1];
  if (!previous) return;
  undoStack.value = undoStack.value.slice(0, -1);
  redoStack.value = [...redoStack.value, currentSnapshot()];
  applySnapshot(previous);
};

const redo = () => {
  const next = redoStack.value[redoStack.value.length - 1];
  if (!next) return;
  redoStack.value = redoStack.value.slice(0, -1);
  undoStack.value = [...undoStack.value, currentSnapshot()];
  applySnapshot(next);
};

/* ------------------------------------------------------------- models */
const loadModels = () =>
  getOmpModels()
    .then((result) => { models.value = result.models; modelError.value = ''; })
    .catch((error: unknown) => { modelError.value = error instanceof Error ? error.message : 'Unable to load models'; });

onMounted(() => { void loadModels(); });

/* --------------------------------------------------------------- graph */
const nodes = computed<Node[]>(() => {
  const workers = steps.value.map((step, index) => ({
    id: step.id,
    type: 'agent',
    position: step.position || positionFor(index),
    data: { label: step.name },
    class: step.id === selectedNode.value ? 'is-selected' : undefined,
    connectable: true,
    deletable: true,
  } satisfies Node));
  const root: Node = {
    id: 'root', type: 'root', position: { x: -220, y: 120 }, data: { label: 'Root orchestrator' },
    class: selectedNode.value === 'root' ? 'is-selected' : undefined,
    draggable: false, selectable: true, connectable: false, deletable: false,
  };
  return [root, ...workers];
});

const edges = computed<Edge[]>(() => {
  const rootEdges = steps.value
    .filter((step) => step.depends_on.length === 0)
    .map((step) => ({
      id: `root=>${step.id}`, source: 'root', target: step.id, sourceHandle: 'out', targetHandle: 'in',
      selectable: false, deletable: false,
    }) satisfies Edge);
  const depEdges = steps.value.flatMap((step) =>
    step.depends_on.map((source) => ({
      id: `${source}=>${step.id}`, source, target: step.id, sourceHandle: 'out', targetHandle: 'in',
      class: `${source}=>${step.id}` === selectedEdgeId.value ? 'is-selected' : undefined,
      selectable: true, deletable: true,
    }) satisfies Edge),
  );
  return [...rootEdges, ...depEdges];
});

const selectedStep = computed(() => steps.value.find((step) => step.id === selectedNode.value));

const updateSelected = (changes: Partial<PipelineStep>) => {
  if (!selectedNode.value) return;
  steps.value = steps.value.map((step) =>
    step.id === selectedNode.value ? { ...step, ...changes } : step,
  );
};

const edgeEndpoints = (edgeId: string) => {
  const marker = edgeId.indexOf('=>');
  return { source: edgeId.slice(0, marker), target: edgeId.slice(marker + 2) };
};

let dragStartSnapshot: Snapshot | null = null;

const onNodesChange = (changes: NodeChange[]) => {
  const moves = changes.filter(
    (change): change is Extract<NodeChange, { type: 'position' }> => change.type === 'position' && !!change.position,
  );
  if (moves.length) {
    if (!dragStartSnapshot) dragStartSnapshot = currentSnapshot();
    steps.value = steps.value.map((step) => {
      const move = moves.find((candidate) => candidate.id === step.id);
      return move?.position ? { ...step, position: { x: move.position.x, y: move.position.y } } : step;
    });
  }
  const removed = changes
    .filter((change): change is Extract<NodeChange, { type: 'remove' }> => change.type === 'remove')
    .map((change) => change.id)
    .filter((id) => id !== 'root');
  if (removed.length) {
    commit(() => {
      let next = steps.value;
      for (const id of removed) next = removeStep(next, id);
      steps.value = next;
    });
    if (selectedNode.value && removed.includes(selectedNode.value)) selectedNode.value = null;
    selectedEdgeId.value = null;
  }
  for (const change of changes) {
    if (change.type !== 'select') continue;
    selectedNode.value = change.selected ? change.id : selectedNode.value === change.id ? null : selectedNode.value;
    if (change.selected) selectedEdgeId.value = null;
  }
};

const onNodeDragStop = () => {
  if (dragStartSnapshot && !snapshotEquals(dragStartSnapshot, currentSnapshot())) {
    pushHistory(dragStartSnapshot);
  }
  dragStartSnapshot = null;
};

const onEdgesChange = (changes: EdgeChange[]) => {
  const removed = changes
    .filter((change): change is Extract<EdgeChange, { type: 'remove' }> => change.type === 'remove')
    .map((change) => edgeEndpoints(change.id))
    .filter((endpoints) => endpoints.source !== 'root');
  if (removed.length) {
    commit(() => {
      let next = steps.value;
      for (const endpoints of removed) next = removeDependency(next, endpoints.source, endpoints.target);
      steps.value = next;
    });
    selectedEdgeId.value = null;
  }
  for (const change of changes) {
    if (change.type !== 'select' || edgeEndpoints(change.id).source === 'root') continue;
    selectedEdgeId.value = change.selected ? change.id : selectedEdgeId.value === change.id ? null : selectedEdgeId.value;
    if (change.selected) selectedNode.value = null;
  }
};

const onConnect = (connection: Connection) => {
  const { source, target } = connection;
  if (!source || !target) return;
  if (source === 'root' || target === 'root') {
    connectNotice.value = 'Root links are automatic for agents without dependencies.';
    return;
  }
  const reason = connectionError(steps.value, source, target);
  if (reason) {
    connectNotice.value = reason;
    return;
  }
  connectNotice.value = '';
  commit(() => { steps.value = connectSteps(steps.value, source, target); });
};

const addAgent = (position?: { x: number; y: number }) => {
  if (steps.value.length >= 64) {
    connectNotice.value = 'Profiles may contain at most 64 agents.';
    return;
  }
  const id = nodeId();
  commit(() => {
    steps.value = [
      ...steps.value,
      {
        id, name: 'New agent', model_selector: models.value[0]?.selector || '', prompt: '',
        depends_on: [], position: position ?? positionFor(steps.value.length),
      },
    ];
  });
  selectedNode.value = id;
  selectedEdgeId.value = null;
};

const deleteSelectedAgent = () => {
  if (!selectedNode.value || selectedNode.value === 'root') return;
  const id = selectedNode.value;
  commit(() => { steps.value = removeStep(steps.value, id); });
  selectedNode.value = null;
};

const removeConnection = () => {
  if (!selectedEdgeId.value) return;
  const { source, target } = edgeEndpoints(selectedEdgeId.value);
  commit(() => { steps.value = removeDependency(steps.value, source, target); });
  selectedEdgeId.value = null;
};

/* ------------------------------------------------------- palette drag */
const onPaletteDragStart = (event: DragEvent) => {
  event.dataTransfer?.setData('application/x-orches-node', 'agent');
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copy';
};

const onCanvasDragOver = (event: DragEvent) => {
  if (event.dataTransfer?.types.includes('application/x-orches-node')) {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }
};

const onCanvasDrop = (event: DragEvent) => {
  if (!event.dataTransfer?.types.includes('application/x-orches-node')) return;
  event.preventDefault();
  const bounds = canvasEl.value?.getBoundingClientRect();
  const position = bounds
    ? project({ x: event.clientX - bounds.left, y: event.clientY - bounds.top })
    : undefined;
  addAgent(position);
};

/* ---------------------------------------------------------- actions */
const autoLayout = () => {
  commit(() => { steps.value = layoutProfileSteps(steps.value); });
  void nextTick(() => fitView({ padding: 0.15 }));
};

const validate = () => {
  validation.value = validateProfileDraft(name.value, steps.value, rootPrompt.value);
  if (!validation.value.length) connectNotice.value = 'Profile is valid.';
};

const save = async () => {
  if (saving.value) return;
  saving.value = true;
  saveError.value = '';
  conflict.value = false;
  try {
    const saved = await updatePipelineProfile(props.profile.id, {
      id: props.profile.id, name: name.value.trim(), root_prompt: rootPrompt.value,
      root_model: rootModel.value, steps: steps.value, revision: revision.value,
    });
    revision.value = saved.revision ?? revision.value + 1;
    baseline.value = snapshotOf(saved);
    applySnapshot(baseline.value);
    savedAt.value = Date.now();
    validation.value = [];
    emit('saved', saved);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unable to save profile';
    if (message.startsWith('HTTP 409')) {
      conflict.value = true;
      saveError.value = 'This profile changed elsewhere. Your draft is untouched — reload the saved version or keep editing.';
    } else {
      saveError.value = message;
    }
  } finally {
    saving.value = false;
  }
};

const reloadServerVersion = async () => {
  try {
    const fresh = await getPipelineProfile(props.profile.id);
    revision.value = fresh.revision ?? revision.value;
    baseline.value = snapshotOf(fresh);
    pushHistory(currentSnapshot());
    applySnapshot(baseline.value);
    conflict.value = false;
    saveError.value = '';
    emit('saved', fresh);
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : 'Unable to reload profile';
  }
};

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);
// Set once the user already approved discarding, so the route-leave guard
// triggered by the same navigation does not ask a second time.
let leaveApproved = false;

const confirmDiscard = async (): Promise<boolean> => {
  const accepted = await confirmDialog.value?.ask({
    title: 'Unsaved changes',
    message: 'Discard unsaved profile changes?',
    confirmLabel: 'Discard changes',
    cancelLabel: 'Keep editing',
    danger: true,
  });
  return Boolean(accepted);
};

const close = async () => {
  if (dirty.value && !(await confirmDiscard())) return;
  leaveApproved = true;
  emit('cancel');
};

/* ------------------------------------------------- guards & keyboard */
const onBeforeUnload = (event: BeforeUnloadEvent) => {
  // Browsers only allow their built-in prompt for page unload.
  if (dirty.value) event.preventDefault();
};
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload));
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload));

onBeforeRouteLeave(async () => {
  if (leaveApproved || !dirty.value) return true;
  return await confirmDiscard();
});

const isFormField = (target: EventTarget | null) =>
  target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);

const onKeydown = (event: KeyboardEvent) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    if (!isFormField(event.target)) {
      event.preventDefault();
      void save();
    }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z' && !isFormField(event.target)) {
    event.preventDefault();
    if (event.shiftKey) redo();
    else undo();
    return;
  }
  if (event.key === 'Escape' && !isFormField(event.target)) {
    selectedNode.value = null;
    selectedEdgeId.value = null;
  }
};

const labelFor = (id: string | null) => {
  if (!id) return id;
  return steps.value.find((candidate) => candidate.id === id)?.name || id;
};

const selectedEndpoints = computed(() => (selectedEdgeId.value ? edgeEndpoints(selectedEdgeId.value) : null));
const selectedAgent = computed(() => selectedStep.value || ({} as PipelineStep));
const selectedModelMissing = computed(() =>
  Boolean(selectedStep.value?.model_selector)
  && !models.value.some((model) => model.selector === selectedStep.value?.model_selector));
const rootModelMissing = computed(() =>
  Boolean(rootModel.value) && !models.value.some((model) => model.selector === rootModel.value));

const status = computed(() => {
  if (saving.value) return { text: 'Saving…', kind: '' };
  if (saveError.value) return { text: 'Save failed', kind: 'is-error' };
  if (dirty.value) return { text: 'Unsaved changes', kind: 'is-dirty' };
  if (savedAt.value) return { text: 'Saved', kind: '' };
  return { text: 'Up to date', kind: '' };
});

// Drive the Vue Flow store directly so edge/node updates always sync, even when the
// graph mounts empty (prop-only binding never creates the edge layer in that case).
const flowId = `profile-editor-${crypto.randomUUID().slice(0, 8)}`;
const { setNodes, setEdges, project, fitView } = useVueFlow(flowId);
watch(nodes, (value) => setNodes(value), { immediate: true });
watch(edges, (value) => setEdges(value), { immediate: true });
</script>

<template>
  <div ref="editorRoot" class="editor-shell" @keydown="onKeydown">
    <ConfirmDialog ref="confirmDialog" />
    <div class="editor-toolbar">
      <button class="control" type="button" @click="close">← Back</button>
      <input
        class="control editor-name"
        aria-label="Profile name"
        :value="name"
        @focus="beginTextEdit"
        @blur="endTextEdit"
        @input="name = ($event.target as HTMLInputElement).value"
      >
      <span :class="`editor-status ${status.kind}`" role="status">{{ status.text }}</span>
      <span class="toolbar-spacer" />
      <button class="control" type="button" :disabled="!undoStack.length" @click="undo">Undo</button>
      <button class="control" type="button" :disabled="!redoStack.length" @click="redo">Redo</button>
      <button class="control" type="button" :disabled="!steps.length" @click="autoLayout">Auto-layout</button>
      <button class="control" type="button" @click="() => fitView({ padding: 0.15 })">Fit view</button>
      <button class="control" type="button" @click="validate">Validate</button>
      <button class="control control-primary" type="button" :disabled="saving" @click="() => void save()">{{ saving ? 'Saving…' : 'Save profile' }}</button>
    </div>
    <div v-if="modelError || saveError || connectNotice || validation.length" class="editor-messages">
      <div v-if="modelError" class="error-state" role="alert">
        <span>{{ modelError }}</span>
        <button class="control" @click="loadModels">Retry</button>
      </div>
      <div v-if="saveError" class="error-state" role="alert">
        <span>{{ saveError }}</span>
        <span v-if="conflict" class="project-head-actions">
          <button class="control" type="button" @click="() => void reloadServerVersion()">Reload saved version</button>
          <button class="control" type="button" @click="() => { conflict = false; saveError = ''; }">Keep my draft</button>
        </span>
      </div>
      <ul v-if="validation.length" class="validation-list" role="alert">
        <li v-for="problem in validation" :key="problem">{{ problem }}</li>
      </ul>
      <p v-if="connectNotice && !validation.length" class="inline-note" role="status">{{ connectNotice }}</p>
    </div>
    <div class="editor-body">
      <aside class="editor-palette" aria-label="Node palette">
        <h3>Nodes</h3>
        <button
          class="palette-item"
          type="button"
          draggable="true"
          @click="() => addAgent()"
          @dragstart="onPaletteDragStart"
        >
          <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c1.4-3.2 4-5 7-5s5.6 1.8 7 5" /></svg></span>
          Agent
        </button>
        <span class="palette-hint">Click to add, or drag onto the canvas. Connect the right handle of one agent to the left handle of another to add a dependency.</span>
      </aside>
      <div ref="canvasEl" class="editor-canvas" @dragover="onCanvasDragOver" @drop="onCanvasDrop">
        <ClientOnly>
          <VueFlow
            :id="flowId"
            :apply-default="false"
            :connection-mode="ConnectionMode.Strict"
            :delete-key-code="['Backspace', 'Delete']"
            :edges-updatable="false"
            fit-view-on-init
            @connect="onConnect"
            @nodes-change="onNodesChange"
            @node-drag-stop="onNodeDragStop"
            @edges-change="onEdgesChange"
          >
            <template #node-agent="agentProps">
              <ProfileAgentNode :data="agentProps.data" :connectable="Boolean(agentProps.connectable)" />
            </template>
            <template #node-root="rootProps">
              <ProfileRootNode :data="rootProps.data" />
            </template>
            <Background pattern-color="var(--canvas-dot)" :gap="18" />
            <Controls />
            <MiniMap pannable zoomable />
          </VueFlow>
        </ClientOnly>
      </div>
      <aside class="editor-inspector" aria-label="Inspector">
        <div v-if="selectedEndpoints" class="profile-edge-actions">
          <h3>Connection</h3>
          <span class="inline-note">{{ labelFor(selectedEndpoints.source) }} → {{ labelFor(selectedEndpoints.target) }}</span>
          <button class="control" type="button" @click="removeConnection">Remove connection</button>
        </div>
        <div v-else-if="selectedNode === 'root'" class="profile-root-panel">
          <h3>Root orchestrator</h3>
          <span class="inline-note">This instruction is appended to the built-in orchestration and safety prompt; it never replaces the host-enforced tool contract or dependency rules.</span>
          <label>Orchestrator model<select
            class="control"
            :value="rootModel"
            @focus="beginTextEdit"
            @blur="endTextEdit"
            @change="rootModel = ($event.target as HTMLSelectElement).value"
          >
            <option value="">Server default ([roles.root] in orches.toml)</option>
            <option v-if="rootModelMissing" :value="rootModel">{{ rootModel }} (unavailable)</option>
            <option v-for="model in models" :key="model.selector" :value="model.selector">{{ model.name }} · {{ model.provider }}</option>
          </select></label>
          <label>Orchestrator prompt<textarea
            class="control"
            rows="8"
            :value="rootPrompt"
            @focus="beginTextEdit"
            @blur="endTextEdit"
            @input="rootPrompt = ($event.target as HTMLTextAreaElement).value"
          /></label>
        </div>
        <span v-else-if="!selectedStep" class="inline-note">Select an agent to edit its name, model and prompt; select the root node for the orchestrator prompt; select a connection to remove it.</span>
        <template v-else>
          <div class="profile-node-panel-head">
            <h3>Agent</h3>
            <button class="control" type="button" @click="deleteSelectedAgent">Delete agent</button>
          </div>
          <label>Name<input
            class="control"
            :value="selectedAgent.name"
            @focus="beginTextEdit"
            @blur="endTextEdit"
            @input="updateSelected({ name: ($event.target as HTMLInputElement).value })"
          ></label>
          <label>Model<select
            class="control"
            :value="selectedAgent.model_selector"
            @focus="beginTextEdit"
            @blur="endTextEdit"
            @change="updateSelected({ model_selector: ($event.target as HTMLSelectElement).value })"
          >
            <option value="">Select a model</option>
            <option v-if="selectedModelMissing" :value="selectedAgent.model_selector">{{ selectedAgent.model_selector }} (unavailable)</option>
            <option v-for="model in models" :key="model.selector" :value="model.selector">{{ model.name }} · {{ model.provider }}</option>
          </select></label>
          <label>Prompt<textarea
            class="control"
            rows="8"
            :value="selectedAgent.prompt"
            @focus="beginTextEdit"
            @blur="endTextEdit"
            @input="updateSelected({ prompt: ($event.target as HTMLTextAreaElement).value })"
          /></label>
          <span class="inline-note mono">{{ selectedAgent.id }}</span>
        </template>
      </aside>
    </div>
  </div>
</template>
