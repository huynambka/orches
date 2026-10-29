import { configureApiBaseUrl, setUnauthorizedHandler } from '~/lib/api';

/**
 * Configure the API transport once per page load: direct browser->backend
 * origin when NUXT_PUBLIC_API_BASE_URL is set (separate-host deployment),
 * otherwise the same-origin Nuxt proxy. Also route 401s back to login.
 */
export default defineNuxtPlugin(() => {
  const config = useRuntimeConfig();
  configureApiBaseUrl(String(config.public.apiBaseUrl || ''));

  const router = useRouter();
  setUnauthorizedHandler(() => {
    const current = router.currentRoute.value;
    if (current.path === '/login') return;
    void router.push({ path: '/login', query: { next: current.fullPath } });
  });
});
