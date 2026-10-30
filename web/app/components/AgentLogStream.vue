<script setup lang="ts">
import { getLogs } from '~/lib/api';
import { appendLogPage } from '~/lib/logs';
import { appendConversation, type ConversationEntry } from '~/lib/conversation';
import type { AgentInvocation } from '~/lib/types';

const props = defineProps<{ runId: string; agent: AgentInvocation; stream: 'stdout' | 'stderr' }>();

const lines = ref<string[]>([]);
const entries = ref<ConversationEntry[]>([]);
const conversationEl = ref<HTMLElement | null>(null);
// Follow new messages only while the reader is at (or near) the bottom; scrolling
// up pauses the follow, scrolling back down resumes it.
const pinned = ref(true);

// Render only the newest entries; scrolling to the top reveals older ones in
// steps without losing the reader's place.
const INITIAL_VISIBLE = 10;
const LOAD_STEP = 10;
const visibleCount = ref(INITIAL_VISIBLE);
const visibleEntries = computed(() => entries.value.slice(-visibleCount.value));
const firstVisibleIndex = computed(() => entries.value.length - visibleEntries.value.length);
const hiddenCount = computed(() => firstVisibleIndex.value);
let loadingOlder = false;

const loadOlder = async () => {
  const el = conversationEl.value;
  if (!el || loadingOlder || !hiddenCount.value) return;
  loadingOlder = true;
  try {
    const previousHeight = el.scrollHeight;
    const previousTop = el.scrollTop;
    visibleCount.value = Math.min(entries.value.length, visibleCount.value + LOAD_STEP);
    await nextTick();
    // Keep the reader anchored on the message they were looking at.
    el.scrollTop = previousTop + (el.scrollHeight - previousHeight);
  } finally {
    loadingOlder = false;
  }
};

const onConversationScroll = () => {
  const el = conversationEl.value;
  if (!el) return;
  pinned.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  if (el.scrollTop <= 40 && hiddenCount.value) void loadOlder();
};

watch(entries, async (next, previous) => {
  if (!pinned.value) {
    // Arrivals while reading history must not shift the visible window start.
    visibleCount.value = Math.min(next.length, visibleCount.value + Math.max(0, next.length - previous.length));
    return;
  }
  await nextTick();
  const el = conversationEl.value;
  if (el) el.scrollTop = el.scrollHeight;
});
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
    <div ref="conversationEl" class="conversation" :aria-label="`${agent.role.replaceAll('_', ' ')} conversation`" @scroll.passive="onConversationScroll">
      <template v-if="entries.length">
        <button v-if="hiddenCount" class="conversation-older" type="button" @click="() => void loadOlder()">
          ↑ {{ hiddenCount }} older {{ hiddenCount === 1 ? 'message' : 'messages' }} — scroll up or click to load
        </button>
        <template v-for="(entry, index) in visibleEntries" :key="firstVisibleIndex + index">
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
          <details v-else-if="entry.kind === 'thinking'" class="conversation-thinking">
            <summary>
              <span class="conversation-tool-icon" aria-hidden="true">∴</span>
              <strong>Thinking</strong>
              <span v-if="entry.pending" class="conversation-pending">Thinking…</span>
            </summary>
            <div class="conversation-text">{{ entry.text }}</div>
          </details>
          <details v-else-if="entry.kind === 'user'" class="conversation-input">
            <summary>Input to agent <span class="prompt-closed">Show prompt</span><span class="prompt-open">Hide prompt</span></summary>
            <div class="conversation-text">{{ textOf(entry) }}</div>
          </details>
          <article v-else :class="`conversation-message conversation-${entry.kind}`">
            <div class="conversation-speaker">{{ entry.kind === 'assistant' ? 'Agent' : 'Output' }}<span v-if="entry.pending"> · writing…</span></div>
            <div v-if="entry.kind === 'assistant' && structuredOf(entry) !== undefined" class="conversation-structured"><StructuredValue :value="structuredOf(entry)" /></div>
            <AgentMarkdown v-else-if="entry.kind === 'assistant'" :text="entry.text" />
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
