<script setup lang="ts">
import {
  createConnection, deleteConnection, getConnections,
  revealConnectionCredential, updateConnection,
} from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import type { Connection, ConnectionChanges } from '~/lib/types';

interface Draft { name: string; os: string; ip: string; cred: string }

const emptyDraft: Draft = { name: '', os: '', ip: '', cred: '' };

const connections = ref<Connection[]>([]);
const loading = ref(true);
const error = ref('');
const formError = ref('');
const saving = ref(false);
const editingId = ref<string | null>(null);
const draft = ref<Draft>({ ...emptyDraft });
const clearCred = ref(false);
const revealedId = ref<string | null>(null);
const revealedCred = ref<string | null>(null);
const revealRequest = { current: null as AbortController | null };

const load = async () => {
  try {
    connections.value = (await getConnections()).connections;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load connections';
  } finally {
    loading.value = false;
  }
};

onMounted(() => { void load(); });
onUnmounted(() => { revealRequest.current?.abort(); });

const hideCredential = () => {
  revealRequest.current?.abort();
  revealRequest.current = null;
  revealedId.value = null;
  revealedCred.value = null;
};

const resetForm = () => {
  editingId.value = null;
  draft.value = { ...emptyDraft };
  clearCred.value = false;
  formError.value = '';
};

const startEdit = (connection: Connection) => {
  hideCredential();
  editingId.value = connection.connection_id;
  draft.value = { name: connection.name, os: connection.os, ip: connection.ip || '', cred: '' };
  clearCred.value = false;
  formError.value = '';
};

const submit = async (event: Event) => {
  event.preventDefault();
  if (saving.value) return;
  if (!draft.value.name.trim() || !draft.value.os.trim()) {
    formError.value = 'Name and OS are required.';
    return;
  }
  saving.value = true;
  formError.value = '';
  try {
    if (editingId.value) {
      const changes: ConnectionChanges = {
        name: draft.value.name.trim(), os: draft.value.os.trim(), ip: draft.value.ip.trim() || null,
      };
      if (draft.value.cred) changes.cred = draft.value.cred;
      else if (clearCred.value) changes.cred = null;
      await updateConnection(editingId.value, changes);
    } else {
      await createConnection({
        name: draft.value.name.trim(), os: draft.value.os.trim(),
        ip: draft.value.ip.trim() || null, cred: draft.value.cred || null,
      });
    }
    hideCredential();
    resetForm();
    await load();
  } catch (caught) {
    formError.value = caught instanceof Error ? caught.message : 'Unable to save connection';
  } finally {
    saving.value = false;
  }
};

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);

const remove = async (connection: Connection) => {
  const accepted = await confirmDialog.value?.ask({
    title: 'Remove connection',
    message: `Remove "${connection.name}" from the connection list? Nothing on that machine is changed.`,
    confirmLabel: 'Remove',
    danger: true,
  });
  if (!accepted) return;
  formError.value = '';
  try {
    await deleteConnection(connection.connection_id);
    if (editingId.value === connection.connection_id) resetForm();
    hideCredential();
    await load();
  } catch (caught) {
    formError.value = caught instanceof Error ? caught.message : 'Unable to remove connection';
  }
};

const reveal = async (connection: Connection) => {
  if (revealedId.value === connection.connection_id) {
    hideCredential();
    return;
  }
  revealRequest.current?.abort();
  const controller = new AbortController();
  revealRequest.current = controller;
  revealedId.value = connection.connection_id;
  revealedCred.value = null;
  try {
    const result = await revealConnectionCredential(connection.connection_id, controller.signal);
    if (controller.signal.aborted) return;
    revealedCred.value = result.cred;
  } catch (caught) {
    if (controller.signal.aborted) return;
    revealedId.value = null;
    formError.value = caught instanceof Error ? caught.message : 'Unable to reveal credential';
  }
};

const usageLabel = (connection: Connection) =>
  connection.in_use && connection.used_by
    ? `Using · ${connection.used_by.role} (run ${connection.used_by.run_id.slice(0, 8)})`
    : 'Available';
</script>

<template>
  <main class="page-shell"><div class="page">
    <ConfirmDialog ref="confirmDialog" />
    <div class="page-header">
      <h1 class="detail-title">Connections</h1>
      <span class="page-help">Address book of machines your agents work with — pipeline agents can list these and reveal a credential via host tools; Orches itself never connects to them.</span>
    </div>
    <div v-if="error" class="error-state" role="alert"><span>{{ error }}</span><button class="control" @click="() => void load()">Retry</button></div>
    <section class="panel project-config" aria-label="Connection form">
      <div class="project-config-heading"><div>
        <h2 class="page-heading">{{ editingId ? 'Edit connection' : 'New connection' }}</h2>
        <p class="inline-note">Credentials are stored in plaintext in the local SQLite database. Anyone with access to that file or this local API can read them.</p>
      </div></div>
      <form class="project-form" @submit="(event) => void submit(event)">
        <div class="project-form-row">
          <label>Name<input class="control" required :maxlength="160" v-model="draft.name" placeholder="Windows analysis VM" /></label>
          <label>OS<input class="control" required :maxlength="100" v-model="draft.os" placeholder="Windows 11 23H2" /></label>
        </div>
        <div class="project-form-row">
          <label>IP address<input class="control" v-model="draft.ip" placeholder="192.0.2.10" /></label>
          <label>Credential<input class="control" type="password" autocomplete="new-password" :maxlength="4096" v-model="draft.cred" :placeholder="editingId ? 'Leave blank to keep the stored credential' : 'analyst / password'" /></label>
        </div>
        <div v-if="editingId" class="project-form-actions">
          <button class="control" type="button" :disabled="saving || Boolean(draft.cred)" @click="() => clearCred = !clearCred">{{ clearCred ? 'Credential will be cleared' : 'Clear credential' }}</button>
          <span v-if="clearCred" class="inline-note" role="status">Saving will delete the stored credential.</span>
        </div>
        <div class="project-form-actions">
          <button class="control control-primary" type="submit" :disabled="saving">{{ saving ? 'Saving…' : editingId ? 'Save changes' : 'Add connection' }}</button>
          <button v-if="editingId" class="control" type="button" :disabled="saving" @click="resetForm">Cancel</button>
        </div>
        <p v-if="formError" class="create-error" role="alert">{{ formError }}</p>
      </form>
    </section>
    <section class="panel" aria-label="Connections">
      <div class="panel-toolbar">
        <span class="toolbar-count">{{ connections.length }} connections</span>
        <span class="toolbar-spacer" />
        <button class="control" :disabled="loading" @click="() => void load()">{{ loading ? 'Refreshing…' : 'Refresh' }}</button>
      </div>
      <div v-if="loading && !connections.length" class="empty-state"><strong>Loading connections…</strong></div>
      <div v-else-if="connections.length" class="tableWrap"><table>
        <thead><tr><th scope="col">Name</th><th scope="col">OS</th><th scope="col">IP</th><th scope="col">Credential</th><th scope="col">Usage</th><th scope="col">Updated</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>
          <tr v-for="connection in connections" :key="connection.connection_id">
            <td class="row-title">{{ connection.name }}</td>
            <td class="secondary-text">{{ connection.os }}</td>
            <td class="secondary-text mono">{{ connection.ip || '—' }}</td>
            <td class="secondary-text">
              <template v-if="connection.has_cred">
                <span v-if="revealedId === connection.connection_id" class="mono">{{ revealedCred === null ? 'Loading…' : revealedCred }}</span>
                <template v-else>Set</template>
              </template>
              <template v-else>Not set</template>
            </td>
            <td class="secondary-text" :title="connection.in_use && connection.used_by ? `run ${connection.used_by.run_id}` : undefined">{{ usageLabel(connection) }}</td>
            <td class="secondary-text">{{ new Date(connection.updated_at).toLocaleString() }}</td>
            <td>
              <div class="project-head-actions">
                <button v-if="connection.has_cred" class="control" type="button" @click="() => void reveal(connection)">{{ revealedId === connection.connection_id ? 'Hide' : 'Reveal' }}</button>
                <button class="control" type="button" :disabled="connection.in_use" :title="connection.in_use ? 'In use by an agent' : undefined" @click="() => startEdit(connection)">Edit</button>
                <button class="control" type="button" :disabled="connection.in_use" :title="connection.in_use ? 'In use by an agent' : undefined" @click="() => void remove(connection)">Remove</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table></div>
      <div v-else class="empty-state"><strong>No connections</strong><span>Record the machines you analyze on: name, OS, IP and credential.</span></div>
    </section>
  </div></main>
</template>
