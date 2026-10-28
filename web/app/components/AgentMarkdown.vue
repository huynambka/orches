<script setup lang="ts">
import { renderMarkdown } from '~/lib/markdown';

const props = defineProps<{ text: string }>();
const html = computed(() => renderMarkdown(props.text));
</script>

<template>
  <!-- Only renderer-generated HTML is allowed; raw agent HTML is escaped. -->
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="conversation-markdown" v-html="html" />
</template>
