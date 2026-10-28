<script setup lang="ts">
import { sendAgentMessage } from '~/lib/api';
import type { AgentId, AgentLayout, AgentStatus, RunDetail } from '~/lib/types';

const props = defineProps<{ projectId: string; runId: string; detail: RunDetail; layout: AgentLayout }>();
const emit = defineEmits<{ refresh: [] }>();

const profile = computed(() => props.detail.pipeline_profile);
const steps = computed(() => profile.value?.steps ?? []);
const nameById = computed(() => {
  const result: Record<string, string> = { root: 'Orchestrator' };
  for (const step of steps.value) result[step.id] = step.name;
  return result;
});
const labelFor = (id: string) => nameById.value[id] || id.replace(/^node_/, 'Agent ');

const invocationFor = (role: AgentId) => [...props.detail.agents].reverse().find((agent) => agent.role === role);

const statuses = computed(() => {
  const result: Partial<Record<AgentId, AgentStatus>> = {};
  for (const role of ['root', ...steps.value.map((step) => step.id)]) {
    result[role] = invocationFor(role)?.status ?? 'pending';
  }
  return result;
});

const selected = ref<AgentId>('root');
const invocation = computed(() => invocationFor(selected.value));
const artifactsFor = computed(() => {
  const current = invocation.value;
  if (!current) return [];
  const ids = new Set([current.output_artifact_id, current.stderr_artifact_id].filter(Boolean));
  return props.detail.artifacts.filter((artifact) => ids.has(artifact.artifact_id));
});

const stageRows = computed(() =>
  [...props.detail.agents].sort((a, b) => (a.started_at < b.started_at ? -1 : 1)),
);

type DockTab = 'conversation' | 'diagnostics' | 'details';
const dockTab = ref<DockTab>('conversation');
const dockOpen = ref(true);

const draft = ref('');
const sending = ref(false);
const message = ref('');

const submit = async () => {
  const current = invocation.value;
  if (!current || !draft.value.trim() || sending.value || !current.chat_available) return;
  sending.value = true;
  message.value = '';
  try {
    await sendAgentMessage(props.runId, current.invocation_id, draft.value.trim());
    message.value = selected.value === 'root' ? 'Guidance sent to the orchestrator.' : 'Guidance accepted.';
    draft.value = '';
    emit('refresh');
  } catch (error) {
    message.value = error instanceof Error ? error.message : 'Unable to send message';
  } finally {
    sending.value = false;
  }
};

const formatTime = (value: string | null) => (value ? new Date(value).toLocaleString() : '—');
</script>

<template>
  <section class="agent-workspace" aria-label="Agent workflow">
    <AgentGraph
      :project-id="projectId"
      :steps="steps"
      :layout="layout"
      :statuses="statuses"
      :animated="detail.run.status === 'running'"
      :selected="selected"
      @select="(role) => { selected = role; }"
    />
    <div class="workflow-panel" aria-live="polite">
      <div class="workflow-panel-heading">
        <div>
          <h3>{{ labelFor(selected) }}</h3>
          <span class="inline-note">{{ invocation ? `${invocation.model_selector || 'default model'} · ${invocation.status}` : 'Invocation pending' }}</span>
        </div>
        <span class="project-head-actions">
          <StatusBadge v-if="invocation" :status="invocation.status" />
          <button class="control" type="button" :aria-expanded="dockOpen" @click="dockOpen = !dockOpen">{{ dockOpen ? 'Hide details' : 'Show details' }}</button>
        </span>
      </div>
      <div v-if="!invocation" class="empty-state">
        <strong>Pending</strong>
        <span>This node has not started yet. Select an active node while the pipeline runs.</span>
      </div>
      <template v-else>
        <section v-if="dockOpen" class="panel" aria-label="Agent details">
          <div class="dock-tabs" role="tablist" aria-label="Agent detail view">
            <button :class="`dock-tab ${dockTab === 'conversation' ? 'active' : ''}`" role="tab" :aria-selected="dockTab === 'conversation'" @click="dockTab = 'conversation'">Conversation</button>
            <button :class="`dock-tab ${dockTab === 'diagnostics' ? 'active' : ''}`" role="tab" :aria-selected="dockTab === 'diagnostics'" @click="dockTab = 'diagnostics'">Diagnostics</button>
            <button :class="`dock-tab ${dockTab === 'details' ? 'active' : ''}`" role="tab" :aria-selected="dockTab === 'details'" @click="dockTab = 'details'">Details</button>
          </div>
          <div class="dock-body">
            <AgentLogStream
              v-if="dockTab === 'conversation'"
              :key="`out-${runId}-${invocation.invocation_id}`"
              :run-id="runId"
              :agent="invocation"
              stream="stdout"
            />
            <AgentLogStream
              v-else-if="dockTab === 'diagnostics'"
              :key="`err-${runId}-${invocation.invocation_id}`"
              :run-id="runId"
              :agent="invocation"
              stream="stderr"
            />
            <div v-else class="dock-details">
              <div><strong>Invocation</strong><span class="mono">#{{ invocation.invocation_id }}</span></div>
              <div><strong>Model</strong><span>{{ invocation.model_selector || 'default model' }}</span></div>
              <div><strong>Effort</strong><span>{{ invocation.thinking }}</span></div>
              <div><strong>Started</strong><span>{{ formatTime(invocation.started_at) }}</span></div>
              <div><strong>Finished</strong><span>{{ formatTime(invocation.finished_at) }}</span></div>
              <div><strong>Return code</strong><span>{{ invocation.returncode ?? '—' }}</span></div>
              <div v-if="invocation.error"><strong>Error</strong><span class="inline-error">{{ invocation.error }}</span></div>
              <div><strong>Artifacts</strong><span v-if="artifactsFor.length" class="mono">{{ artifactsFor.map((artifact) => artifact.relative_path).join(', ') }}</span><span v-else>—</span></div>
              <strong>Run stages</strong>
              <ul class="stage-list">
                <li v-for="stage in stageRows" :key="stage.invocation_id" class="stage-row">
                  <StatusBadge :status="stage.status" />
                  <span>{{ labelFor(stage.role) }}</span>
                  <span class="mono">{{ formatTime(stage.started_at) }}</span>
                </li>
              </ul>
            </div>
          </div>
        </section>
        <form class="agent-chat" @submit.prevent="submit">
          <label for="agent-message">
            <strong>{{ selected === 'root' ? 'Guide orchestrator' : 'Guide this active agent' }}</strong>
            <span class="inline-note">Messages are supplemental guidance and do not bypass profile dependencies or safety checks.</span>
          </label>
          <textarea
            id="agent-message"
            class="control"
            :maxlength="4000"
            :value="draft"
            :disabled="!invocation.chat_available || sending"
            :placeholder="invocation.chat_available ? 'Add focused guidance' : 'Chat is unavailable after this agent finishes'"
            @input="draft = ($event.target as HTMLTextAreaElement).value"
          />
          <div class="agent-chat-actions">
            <span class="inline-note">{{ draft.length }}/4000</span>
            <button class="control control-primary" type="submit" :disabled="!invocation.chat_available || sending || !draft.trim()">{{ sending ? 'Sending…' : 'Send guidance' }}</button>
          </div>
          <span v-if="message" class="inline-note" role="status">{{ message }}</span>
        </form>
      </template>
    </div>
  </section>
</template>
