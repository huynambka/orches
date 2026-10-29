<script setup lang="ts">
import type { McpServerConfig } from '~/lib/types';

const props = defineProps<{
  modelValue: McpServerConfig;
  name: string;
  editing: boolean;
  disabled: boolean;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: McpServerConfig];
  'update:name': [value: string];
}>();

const NAME_RE = /^[a-zA-Z0-9_.-]{1,100}$/;

const transport = computed({
  get: () => {
    const t = props.modelValue.type;
    if (t === 'http' || t === 'sse') return t;
    if (typeof props.modelValue.command === 'string') return 'stdio';
    if (typeof props.modelValue.url === 'string') {
      return props.modelValue.type === 'sse' ? 'sse' : 'http';
    }
    return 'stdio';
  },
  set: (value: string) => {
    const shared = pickShared(props.modelValue);
    if (value === 'stdio') emit('update:modelValue', { ...shared, command: '' });
    else emit('update:modelValue', { ...shared, type: value, url: '' });
  },
});

const SHARED_KEYS = ['enabled', 'timeout', 'requestIdFormat', 'instructions', 'auth', 'oauth'] as const;
function pickShared(config: McpServerConfig): McpServerConfig {
  const result: McpServerConfig = {};
  for (const key of SHARED_KEYS) {
    if (key in config) result[key] = config[key];
  }
  return result;
}

function patch(updates: Record<string, unknown>) {
  emit('update:modelValue', { ...props.modelValue, ...updates });
}

// stdio fields
const command = computed({
  get: () => String(props.modelValue.command ?? ''),
  set: (v: string) => patch({ command: v }),
});
const argsText = computed({
  get: () => Array.isArray(props.modelValue.args) ? (props.modelValue.args as string[]).join('\n') : '',
  set: (v: string) => {
    const lines = v ? v.split('\n') : [];
    patch({ args: lines.length ? lines : undefined });
  },
});
const cwd = computed({
  get: () => String(props.modelValue.cwd ?? ''),
  set: (v: string) => patch({ cwd: v || undefined }),
});
const envText = computed({
  get: () => {
    const e = props.modelValue.env;
    return e && typeof e === 'object' ? JSON.stringify(e, null, 2) : '';
  },
  set: (v: string) => {
    if (!v.trim()) { patch({ env: undefined }); return; }
    try { patch({ env: JSON.parse(v) }); envError.value = ''; }
    catch { envError.value = 'Invalid JSON'; }
  },
});
const envError = ref('');

// remote fields (http/sse)
const url = computed({
  get: () => String(props.modelValue.url ?? ''),
  set: (v: string) => patch({ url: v }),
});
const headersText = computed({
  get: () => {
    const h = props.modelValue.headers;
    return h && typeof h === 'object' ? JSON.stringify(h, null, 2) : '';
  },
  set: (v: string) => {
    if (!v.trim()) { patch({ headers: undefined }); return; }
    try { patch({ headers: JSON.parse(v) }); headersError.value = ''; }
    catch { headersError.value = 'Invalid JSON'; }
  },
});
const headersError = ref('');

// Advanced JSON
const showAdvanced = ref(false);
const advancedText = ref('');
const advancedError = ref('');

function openAdvanced() {
  advancedText.value = JSON.stringify(props.modelValue, null, 2);
  advancedError.value = '';
  showAdvanced.value = true;
}

function applyAdvanced() {
  try {
    const parsed = JSON.parse(advancedText.value);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      advancedError.value = 'Config must be a JSON object';
      return;
    }
    emit('update:modelValue', parsed as McpServerConfig);
    advancedError.value = '';
    showAdvanced.value = false;
  } catch {
    advancedError.value = 'Invalid JSON';
  }
}

const nameError = computed(() => {
  if (!props.name) return '';
  return NAME_RE.test(props.name) ? '' : 'Letters, digits, underscores, dots and dashes only (1–100 chars)';
});
</script>

<template>
  <div class="mcp-editor">
    <label v-if="!editing">Server name
      <input
        class="control"
        required
        maxlength="100"
        pattern="^[a-zA-Z0-9_.\-]{1,100}$"
        :value="name"
        :disabled="disabled"
        placeholder="my-server"
        @input="emit('update:name', ($event.target as HTMLInputElement).value)"
      />
      <span v-if="nameError" class="field-error" role="alert">{{ nameError }}</span>
    </label>
    <span v-else class="mcp-editor-name"><strong>Server:</strong> {{ name }}</span>

    <label>Transport
      <select class="control" :value="transport" :disabled="disabled" @change="transport = ($event.target as HTMLSelectElement).value as 'stdio' | 'http' | 'sse'">
        <option value="stdio">stdio</option>
        <option value="http">HTTP</option>
        <option value="sse">SSE</option>
      </select>
    </label>

    <template v-if="transport === 'stdio'">
      <label>Command
        <input class="control" required :value="command" :disabled="disabled" placeholder="/usr/bin/my-server" @input="command = ($event.target as HTMLInputElement).value" />
      </label>
      <label>Arguments <span class="inline-note">(one per line)</span>
        <textarea class="control" :rows="3" :value="argsText" :disabled="disabled" placeholder="--port&#10;3000" @input="argsText = ($event.target as HTMLTextAreaElement).value" />
      </label>
      <label>Working directory
        <input class="control" :value="cwd" :disabled="disabled" placeholder="Optional cwd" @input="cwd = ($event.target as HTMLInputElement).value" />
      </label>
      <label>Environment variables <span class="inline-note">(JSON object)</span>
        <textarea class="control" :rows="2" :value="envText" :disabled="disabled" placeholder='{"KEY": "value"}' @input="envText = ($event.target as HTMLTextAreaElement).value" />
        <span v-if="envError" class="field-error" role="alert">{{ envError }}</span>
      </label>
    </template>

    <template v-else>
      <label>URL
        <input class="control" required type="url" :value="url" :disabled="disabled" placeholder="https://mcp.example.com" @input="url = ($event.target as HTMLInputElement).value" />
      </label>
      <label>Headers <span class="inline-note">(JSON object)</span>
        <textarea class="control" :rows="2" :value="headersText" :disabled="disabled" placeholder='{"Authorization": "Bearer ..."}' @input="headersText = ($event.target as HTMLTextAreaElement).value" />
        <span v-if="headersError" class="field-error" role="alert">{{ headersError }}</span>
      </label>
    </template>

    <div class="mcp-advanced">
      <button class="control" type="button" :disabled="disabled" @click="showAdvanced ? (showAdvanced = false) : openAdvanced()">
        {{ showAdvanced ? 'Hide advanced JSON' : 'Advanced JSON' }}
      </button>
      <template v-if="showAdvanced">
        <textarea class="control mcp-advanced-json" :rows="10" v-model="advancedText" :disabled="disabled" />
        <span v-if="advancedError" class="field-error" role="alert">{{ advancedError }}</span>
        <button class="control" type="button" :disabled="disabled" @click="applyAdvanced">Apply JSON</button>
        <p class="inline-note">Editing the full JSON replaces all fields including shared options (enabled, timeout, auth, etc.).</p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.mcp-editor { display: grid; gap: 10px; }
.mcp-editor-name { font-size: 14px; padding: 4px 0; }
.field-error { color: var(--error); font-size: 12px; display: block; margin-top: 2px; }
.mcp-advanced { display: grid; gap: 8px; margin-top: 4px; }
.mcp-advanced-json { font-family: var(--font-mono, monospace); font-size: 13px; }
</style>
