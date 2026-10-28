<script setup lang="ts">
const props = withDefaults(defineProps<{ value: unknown; depth?: number }>(), { depth: 0 });
const entries = computed(() =>
  props.value !== null && typeof props.value === 'object' && !Array.isArray(props.value)
    ? Object.entries(props.value as Record<string, unknown>)
    : [],
);
</script>

<template>
  <span v-if="value === null || typeof value !== 'object'">{{ String(value ?? '—') }}</span>
  <span v-else-if="depth >= 5" class="conversation-text">{{ JSON.stringify(value, null, 2) }}</span>
  <ul v-else-if="Array.isArray(value)">
    <li v-for="(item, index) in value" :key="index"><StructuredValue :value="item" :depth="depth + 1" /></li>
  </ul>
  <dl v-else>
    <div v-for="[key, item] in entries" :key="key">
      <dt>{{ key.replaceAll('_', ' ') }}</dt>
      <dd><StructuredValue :value="item" :depth="depth + 1" /></dd>
    </div>
  </dl>
</template>
