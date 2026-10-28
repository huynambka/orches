<script setup lang="ts">
import { createPipelineProfile, deletePipelineProfile, getPipelineProfile, getPipelineProfiles } from '~/lib/api';
import ConfirmDialog from '~/components/ConfirmDialog.vue';
import type { PipelineProfile } from '~/lib/types';

const route = useRoute();
const router = useRouter();

const profiles = ref<PipelineProfile[]>([]);
const loading = ref(true);
const error = ref('');

const editing = ref<PipelineProfile | null>(null);
const editorLoading = ref(false);
const editorError = ref('');

const createDialog = ref<HTMLDialogElement | null>(null);
const newName = ref('');
const creating = ref(false);
const createError = ref('');

const editId = computed(() => (typeof route.query.edit === 'string' ? route.query.edit : ''));

const load = async () => {
  try {
    profiles.value = (await getPipelineProfiles()).profiles;
    error.value = '';
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to load profiles';
  } finally {
    loading.value = false;
  }
};

const loadEditor = async (id: string) => {
  editorLoading.value = true;
  editorError.value = '';
  try {
    editing.value = await getPipelineProfile(id);
  } catch (caught) {
    editing.value = null;
    editorError.value = caught instanceof Error ? caught.message : 'Unable to load profile';
  } finally {
    editorLoading.value = false;
  }
};

watch(editId, (id) => {
  if (id) void loadEditor(id);
  else {
    editing.value = null;
    editorError.value = '';
    void load();
  }
}, { immediate: false });

onMounted(() => {
  void load();
  if (editId.value) void loadEditor(editId.value);
});

const openEditor = (id: string) => router.push({ path: '/profiles', query: { edit: id } });
const closeEditor = () => router.push({ path: '/profiles' });

const openCreate = () => {
  newName.value = '';
  createError.value = '';
  createDialog.value?.showModal();
};

const submitCreate = async (event: Event) => {
  event.preventDefault();
  if (creating.value || !newName.value.trim()) return;
  creating.value = true;
  createError.value = '';
  try {
    const profile = await createPipelineProfile(newName.value.trim());
    profiles.value = [...profiles.value, profile];
    createDialog.value?.close();
    void openEditor(profile.id);
  } catch (caught) {
    createError.value = caught instanceof Error ? caught.message : 'Unable to create profile';
  } finally {
    creating.value = false;
  }
};

const confirmDialog = ref<InstanceType<typeof ConfirmDialog> | null>(null);

const remove = async (profile: PipelineProfile) => {
  const accepted = await confirmDialog.value?.ask({
    title: 'Delete profile',
    message: `Delete ${profile.name || profile.id}? Projects keep their own run snapshots.`,
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!accepted) return;
  try {
    await deletePipelineProfile(profile.id);
    profiles.value = profiles.value.filter((item) => item.id !== profile.id);
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to delete profile';
  }
};

const onSaved = (profile: PipelineProfile) => {
  profiles.value = profiles.value.map((item) => (item.id === profile.id ? profile : item));
  editing.value = profile;
};
</script>

<template>
  <ProfileEditor
    v-if="editId && editing"
    :profile="editing"
    @cancel="closeEditor"
    @saved="onSaved"
  />
  <main v-else-if="editId" class="page-shell">
    <div class="page">
      <div v-if="editorLoading" class="panel empty-state"><strong>Loading profile…</strong></div>
      <div v-else class="error-state" role="alert">
        <span>{{ editorError || 'Profile not found.' }}</span>
        <span class="project-head-actions">
          <button class="control" @click="() => void loadEditor(editId)">Retry</button>
          <button class="control" @click="closeEditor">Back to profiles</button>
        </span>
      </div>
    </div>
  </main>
  <main v-else class="page-shell page-shell-narrow">
    <div class="page">
      <ConfirmDialog ref="confirmDialog" />
      <div class="page-heading-row">
        <div>
          <h1 class="page-heading">Profiles</h1>
          <p class="inline-note">Design reusable agent pipelines run by the root orchestrator.</p>
        </div>
        <button class="control control-primary" type="button" @click="openCreate">New profile</button>
      </div>
      <div v-if="error" class="error-state" role="alert">
        <span>{{ error }}</span>
        <button class="control" @click="load">Retry</button>
      </div>
      <dialog ref="createDialog" class="modal" aria-labelledby="create-profile-heading">
        <div class="modal-head">
          <h2 id="create-profile-heading" class="page-heading">New profile</h2>
          <button class="icon-button" type="button" aria-label="Close dialog" :disabled="creating" @click="createDialog?.close()"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg></button>
        </div>
        <form class="create-form modal-body" @submit="(event) => void submitCreate(event)">
          <label class="create-field">Profile name <input class="control" required :maxlength="160" v-model="newName" placeholder="Research pipeline" /></label>
          <p v-if="createError" class="create-error" role="alert">{{ createError }}</p>
          <div class="modal-actions">
            <button class="control" type="button" :disabled="creating" @click="createDialog?.close()">Cancel</button>
            <button class="control control-primary" type="submit" :disabled="creating">{{ creating ? 'Creating…' : 'Create profile' }}</button>
          </div>
        </form>
      </dialog>
      <section class="panel">
        <div class="panel-toolbar">
          <strong>{{ loading ? 'Loading…' : `${profiles.length} ${profiles.length === 1 ? 'profile' : 'profiles'}` }}</strong>
          <span class="toolbar-spacer" />
          <button class="control" @click="load">Refresh</button>
        </div>
        <div v-if="profiles.length === 0 && !loading" class="empty-state">
          <strong>No profiles</strong>
          <span>Create an empty profile, then add and connect agents.</span>
        </div>
        <div v-else class="profile-grid">
          <article v-for="profile in profiles" :key="profile.id" class="profile-card">
            <div class="profile-card-head">
              <span class="profile-node-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="m7.5 11 9-4M7.5 13l9 4" /></svg></span>
              <strong class="profile-card-title">{{ profile.name || profile.id }}</strong>
              <span v-if="profile.steps.length === 0" class="status-badge">Draft</span>
            </div>
            <span class="inline-note">
              {{ profile.steps.length }} {{ profile.steps.length === 1 ? 'agent' : 'agents' }}
              · revision {{ profile.revision ?? 1 }}
            </span>
            <div v-if="profile.steps.length" class="profile-chips" aria-label="Agents">
              <span v-for="step in profile.steps.slice(0, 4)" :key="step.id" class="profile-chip">{{ step.name }}</span>
              <span v-if="profile.steps.length > 4" class="profile-chip profile-chip-more">+{{ profile.steps.length - 4 }}</span>
            </div>
            <span v-else class="inline-note">No agents yet — open the editor to add some.</span>
            <div class="profile-card-actions">
              <button class="control control-primary" type="button" @click="() => void openEditor(profile.id)">Edit</button>
              <button class="control" type="button" @click="remove(profile)">Delete</button>
            </div>
          </article>
        </div>
      </section>
    </div>
  </main>
</template>
