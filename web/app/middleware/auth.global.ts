import { clearToken, getAuthSession, getToken } from '~/lib/api';

// Validate the stored session once per page load; later 401s are handled by
// the shared request helper.
let validated = false;

export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return;
  if (to.path === '/login') return;
  const redirect = { path: '/login', query: { next: to.fullPath } };
  if (!getToken()) return navigateTo(redirect);
  if (!validated) {
    try {
      await getAuthSession();
      validated = true;
    } catch {
      clearToken();
      return navigateTo(redirect);
    }
  }
});
