<script setup lang="ts">
import { getToken, login } from '~/lib/api';
import { safeNextPath } from '~/lib/auth';

const route = useRoute();
const password = ref('');
const error = ref('');
const submitting = ref(false);

onMounted(() => {
  if (getToken()) void navigateTo(safeNextPath(route.query.next), { replace: true });
});

const submit = async (event: Event) => {
  event.preventDefault();
  if (submitting.value || !password.value) return;
  submitting.value = true;
  error.value = '';
  try {
    await login(password.value);
    await navigateTo(safeNextPath(route.query.next), { replace: true });
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Unable to log in';
    password.value = '';
  } finally {
    submitting.value = false;
  }
};
</script>

<template>
  <main class="login-shell">
    <form class="login-card" @submit="(event) => void submit(event)">
      <div class="login-brand"><span class="brand-mark"><BrandMark /></span><span>ORCHES</span></div>
      <h1 class="page-heading">Administrator sign in</h1>
      <p class="inline-note">Enter the administrator password configured on the Orches API host.</p>
      <label>Password
        <input
          class="control"
          type="password"
          autocomplete="current-password"
          autofocus
          required
          :value="password"
          @input="(event) => { password = (event.target as HTMLInputElement).value; }"
        >
      </label>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
      <button class="control control-primary" type="submit" :disabled="submitting || !password">
        {{ submitting ? 'Signing in…' : 'Sign in' }}
      </button>
    </form>
  </main>
</template>
