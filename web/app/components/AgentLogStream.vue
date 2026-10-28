<script setup lang="ts">
import { getLogs } from '~/lib/api';
import { appendLogPage } from '~/lib/logs';
import { appendConversation, type ConversationEntry } from '~/lib/conversation';
import type { AgentInvocation } from '~/lib/types';

const props = defineProps<{ runId: string; agent: AgentInvocation; stream: 'stdout' | 'stderr' }>();

const lines = ref<string[]>([]);
const entries = ref<ConversationEntry[]>([]);
const error = ref('');
let offset = 0;
let active = true;
let fetching = false;
let timer: number | undefined;

const poll = async () => {
  if (fetching) return;
  fetching = true;
  try {
    while (active) {
      const previous = offset;
      const page = await getLogs(props.runId, props.agent.invocation_id, props.stream, previous, 500);
      if (!active) return;
      offset = page.next_offset;
      lines.value = appendLogPage(lines.value, page);
      if (props.stream === 'stdout' && page.lines.length) entries.value = appendConversation(entries.value, page.lines);
      error.value = '';
      if (page.next_offset <= previous || page.lines.length < 500 || page.eof) break;
    }
  } catch (caught) {
    if (active) error.value = caught instanceof Error ? caught.message : 'Unable to load log';
  } finally {
    fetching = false;
  }
};

const startTimer = () => {
  if (timer) { window.clearInterval(timer); timer = undefined; }
  if (props.agent.status === 'running') timer = window.setInterval(() => void poll(), 2000);
};

onMounted(() => {
  active = true;
  void poll();
  startTimer();
});

// Mirror the React effect: re-poll and restart the 2s timer when the invocation status flips.
watch(() => props.agent.status, () => {
  void poll();
  startTimer();
});

onUnmounted(() => {
  active = false;
  if (timer) window.clearInterval(timer);
});

const toolEntry = (entry: ConversationEntry) => (entry.kind === 'tool' ? entry : null);
const structuredOf = (entry: ConversationEntry): unknown => {
  if (entry.kind !== 'assistant' || entry.pending) return undefined;
  try {
    const value: unknown = JSON.parse(entry.text);
    if (value !== null && typeof value === 'object') return value;
  } catch {
    // Partial streamed messages and ordinary prose stay as text.
  }
  return undefined;
};
const textOf = (entry: ConversationEntry) => (entry.kind === 'tool' ? '' : entry.text);
</script>

<template>
  <details v-if="stream === 'stderr'" class="log-raw">
    <summary>Diagnostics (stderr){{ lines.length ? ` · ${lines.length} recent lines` : '' }}</summary>
    <p v-if="error" class="inline-error">{{ error }}</p>
    <pre v-if="lines.length">{{ lines.join('\n') }}</pre>
    <pre v-else><em>No diagnostics recorded.</em></pre>
  </details>
  <div v-else class="log-stream">
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <div class="conversation" :aria-label="`${agent.role.replaceAll('_', ' ')} conversation`">
      <template v-if="entries.length">
        <template v-for="(entry, index) in entries" :key="index">
          <details v-if="toolEntry(entry)" class="conversation-tool">
            <summary>
              <span class="conversation-tool-icon" aria-hidden="true">⌘</span>
              <strong>{{ toolEntry(entry)!.name }}</strong>
              <span>{{ toolEntry(entry)!.intent || (toolEntry(entry)!.pending ? 'Running…' : 'Result') }}</span>
              <span v-if="toolEntry(entry)!.pending" class="conversation-pending">Running…</span>
            </summary>
            <div v-if="toolEntry(entry)!.input" class="conversation-tool-detail"><strong>Input</strong><pre>{{ toolEntry(entry)!.input }}</pre></div>
            <div v-if="toolEntry(entry)!.output" class="conversation-tool-detail"><strong>Result</strong><pre>{{ toolEntry(entry)!.output }}</pre></div>
          </details>
          <details v-else-if="entry.kind === 'user'" class="conversation-input">
            <summary>Input to agent <span class="prompt-closed">Show prompt</span><span class="prompt-open">Hide prompt</span></summary>
            <div class="conversation-text">{{ textOf(entry) }}</div>
          </details>
          <article v-else :class="`conversation-message conversation-${entry.kind}`">
            <div class="conversation-speaker">{{ entry.kind === 'assistant' ? 'Agent' : 'Output' }}<span v-if="entry.pending"> · writing…</span></div>
            <div v-if="entry.kind === 'assistant' && structuredOf(entry) !== undefined" class="conversation-structured"><StructuredValue :value="structuredOf(entry)" /></div>
            <div v-else class="conversation-text">{{ textOf(entry) }}</div>
          </article>
        </template>
      </template>
      <div v-else class="conversation-empty">{{ agent.status === 'running' ? 'Waiting for agent messages…' : 'No messages recorded.' }}</div>
    </div>
    <details class="log-raw">
      <summary>Raw JSONL · last {{ lines.length }} lines</summary>
      <pre v-if="lines.length">{{ lines.join('\n') }}</pre>
      <pre v-else><em>No output recorded.</em></pre>
    </details>
  </div>
</template>
