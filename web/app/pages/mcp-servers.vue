<script setup lang="ts">
import {
  createMcpServer, deleteMcpServer, getMcpServer, getMcpServers, updateMcpServer,
} from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import McpServerEditor from '~/components/McpServerEditor.vue';
import type { McpCatalogEntry, McpServerConfig } from '~/lib/types';

const servers = ref<McpCatalogEntry[]>([]);
const loading = ref(true);
const error = ref('');
const formError = ref('');
const saving = ref(false);
const editingName = ref<string | null>(null);
const editingRevision = ref(0);
const draftName = ref('');
const draftConfig = ref<McpServerConfig>({ command: '' });
const formDialog = ref<HTMLDialogElement | null>(null);

const load = async () => {
  try {
    servers.value = (await getMcpServers()).servers;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load MCP servers';
  } finally {
    loading.value = false;
  }
};

onMounted(() => { void load(); });

const resetForm = () => {
  editingName.value = null;
  editingRevision.value = 0;
  draftName.value = '';
  draftConfig.value = { command: '' };
  formError.value = '';
};

const openCreate = () => {
  resetForm();
  formDialog.value?.showModal();
};

const startEdit = async (entry: McpCatalogEntry) => {
  formError.value = '';
  try {
    const detail = await getMcpServer(entry.name);
    editingName.value = detail.name;
    editingRevision.value = detail.revision;
    draftName.value = detail.name;
    draftConfig.value = { ...detail.config };
    formDialog.value?.showModal();
  } catch (caught) {
    formError.value = caught instanceof Error ? caught.message : 'Unable to load server details';
  }
};

const closeDialog = () => {
  formDialog.value?.close();
  resetForm();
};

const submit = async (event: Event) => {
  event.preventDefault();
  if (saving.value) return;
  const name = draftName.value.trim();
  if (!name) { formError.value = 'Name is required.'; return; }
  saving.value = true;
  formError.value = '';
  try {
    if (editingName.value) {
      await updateMcpServer(editingName.value, draftConfig.value, editingRevision.value);
    } else {
      await createMcpServer(name, draftConfig.value);
    }
    closeDialog();
    await load();
  } catch (caught) {
    formError.value = caught instanceof Error ? caught.message : 'Unable to save server';
  } finally {
    saving.value = false;
  }
};

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);

const remove = async (entry: McpCatalogEntry) => {
  const accepted = await confirmDialog.value?.ask({
    title: 'Remove MCP server',
    message: `Remove "${entry.name}" from the catalog? Existing project copies of this server will not be affected.`,
    confirmLabel: 'Remove',
    danger: true,
  });
  if (!accepted) return;
  error.value = '';
  try {
    await deleteMcpServer(entry.name);
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to remove server';
  }
};
</script>

<template>
  <main class="page-shell"><div class="page">
    <ConfirmDialog ref="confirmDialog" />
    <div class="page-header">
      <h1 class="detail-title">MCP servers</h1>
      <span class="page-help">Reusable MCP server configurations. Adding a catalog entry to a project copies a snapshot — central edits do not automatically update projects.</span>
    </div>
    <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
    <section class="panel" aria-label="MCP server catalog">
      <div class="panel-toolbar">
        <span class="toolbar-count">{{ servers.length }} server{{ servers.length === 1 ? '' : 's' }}</span>
        <span class="toolbar-spacer" />
        <button class="control" :disabled="loading" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button>
        <button class="control control-primary" type="button" @click="openCreate">Add server</button>
      </div>
      <div v-if="loading && !servers.length" class="empty-state"><strong>Loading servers…</strong></div>
      <div v-else-if="servers.length" class="tableWrap"><table>
        <thead><tr><th scope="col">Name</th><th scope="col">Transport</th><th scope="col">Endpoint</th><th scope="col">Revision</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>
          <tr v-for="entry in servers" :key="entry.name">
            <td class="mono">{{ entry.name }}</td>
            <td>{{ entry.transport }}</td>
            <td>{{ entry.endpoint || '—' }}</td>
            <td>{{ entry.revision }}</td>
            <td class="row-actions">
              <button class="control" type="button" @click="() => void startEdit(entry)">Edit</button>
              <button class="control" type="button" @click="() => void remove(entry)">Remove</button>
            </td>
          </tr>
        </tbody>
      </table></div>
      <div v-else class="empty-state"><strong>No MCP servers</strong><span>Add reusable MCP server configurations that can be selected in projects.</span></div>
    </section>

    <!-- Create / Edit dialog -->
    <dialog ref="formDialog" class="modal mcp-form-modal" :aria-label="editingName ? 'Edit MCP server' : 'Add MCP server'" @close="resetForm">
      <div class="modal-head">
        <h2 class="page-heading">{{ editingName ? 'Edit server' : 'New server' }}</h2>
        <button class="control modal-close" type="button" aria-label="Close" @click="closeDialog">✕</button>
      </div>
      <div class="modal-body">
        <p class="inline-note">Credentials in config are stored in plaintext. Use <code>${"${VAR}"}</code> placeholders or OAuth for sensitive values.</p>
        <form class="project-form" @submit="(event) => void submit(event)">
          <McpServerEditor
            v-model="draftConfig"
            :name="draftName"
            :editing="Boolean(editingName)"
            :disabled="saving"
            @update:name="draftName = $event"
          />
          <div class="modal-actions">
            <button class="control" type="button" :disabled="saving" @click="closeDialog">Cancel</button>
            <button class="control control-primary" type="submit" :disabled="saving">{{ saving ? 'Saving…' : editingName ? 'Save changes' : 'Add server' }}</button>
          </div>
          <p v-if="formError" class="create-error" role="alert">{{ formError }}</p>
        </form>
      </div>
    </dialog>
  </div></main>
</template>


