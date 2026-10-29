<script setup lang="ts">
type Theme = 'light' | 'dark' | 'system';
const THEME_KEY = 'orches-theme';
const COLLAPSE_KEY = 'orches-sidebar-collapsed';

const theme = ref<Theme>('light');
const collapsed = ref(false);
const drawer = ref<HTMLDialogElement | null>(null);
const drawerToggle = ref<HTMLButtonElement | null>(null);
const route = useRoute();

const applyTheme = () => {
  if (!import.meta.client) return;
  const dark = theme.value === 'dark'
    || (theme.value === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
};

const setTheme = (next: Theme) => {
  theme.value = next;
  localStorage.setItem(THEME_KEY, next);
  applyTheme();
};

let media: MediaQueryList | undefined;
const onSystemTheme = () => { if (theme.value === 'system') applyTheme(); };

onMounted(() => {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === 'dark' || saved === 'system' || saved === 'light') theme.value = saved;
  collapsed.value = localStorage.getItem(COLLAPSE_KEY) === '1';
  media = window.matchMedia('(prefers-color-scheme: dark)');
  media.addEventListener('change', onSystemTheme);
  applyTheme();
});
onUnmounted(() => { media?.removeEventListener('change', onSystemTheme); });

const toggleSidebar = () => {
  collapsed.value = !collapsed.value;
  localStorage.setItem(COLLAPSE_KEY, collapsed.value ? '1' : '0');
};

const openDrawer = () => drawer.value?.showModal();
const closeDrawer = () => {
  drawer.value?.close();
  drawerToggle.value?.focus();
};
watch(() => route.fullPath, () => { drawer.value?.close(); });

// The profile editor is a distraction-free workbench without the dashboard shell.
const editorMode = computed(() => route.path.startsWith('/profiles') && Boolean(route.query.edit));

const crumbs = computed(() => {
  const path = route.path;
  if (path.startsWith('/profiles')) return [{ label: 'Profiles', to: '/profiles' }];
  if (path.startsWith('/connections')) return [{ label: 'Connections', to: '/connections' }];
  if (path.startsWith('/mcp-servers')) return [{ label: 'MCP servers', to: '/mcp-servers' }];
  if (path.startsWith('/costs')) return [{ label: 'Costs', to: '/costs' }];
  const result = [{ label: 'Projects', to: '/' }];
  const projectId = route.params.projectId;
  if (typeof projectId === 'string' && projectId) {
    result.push({ label: projectId, to: `/projects/${encodeURIComponent(projectId)}` });
  }
  return result;
});
</script>

<template>
  <NuxtPage v-if="editorMode" />
  <div v-else :class="`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`">
    <aside class="sidebar">
      <NuxtLink to="/" class="brand"><span class="brand-mark"><BrandMark /></span><span>ORCHES</span></NuxtLink>
      <div class="nav-label">Workspace</div>
      <MainNav variant="sidebar" :collapsed="collapsed" />
      <div class="sidebar-footer"><strong>Local workspace</strong>Design agent pipelines · run them on your folders</div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="top-left">
          <button
            class="icon-button mobile-nav-toggle"
            type="button"
            aria-label="Open navigation"
            ref="drawerToggle"
            @click="openDrawer"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
          </button>
          <button
            class="icon-button sidebar-toggle"
            type="button"
            :aria-label="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
            :aria-expanded="!collapsed"
            :title="collapsed ? 'Expand sidebar' : 'Collapse sidebar'"
            @click="toggleSidebar"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9.5 4v16" /></svg>
          </button>
          <span class="top-divider" />
          <nav class="breadcrumbs" aria-label="Breadcrumb">
            <template v-for="(crumb, index) in crumbs" :key="crumb.to">
              <span v-if="index" class="crumb-sep" aria-hidden="true">/</span>
              <span v-if="index === crumbs.length - 1" class="crumb-current" aria-current="page">{{ crumb.label }}</span>
              <NuxtLink v-else :to="crumb.to">{{ crumb.label }}</NuxtLink>
            </template>
          </nav>
        </div>
        <div class="top-right">
          <span class="top-pill">Local monitor</span>
          <label class="sr-only" for="theme-select">Theme</label>
          <select
            id="theme-select"
            class="control theme-select"
            :value="theme"
            @change="setTheme(($event.target as HTMLSelectElement).value as 'light' | 'dark' | 'system')"
          >
            <option value="light">Light</option>
            <option value="dark">Dark</option>
            <option value="system">System</option>
          </select>
        </div>
      </header>
      <NuxtPage />
    </div>
    <dialog ref="drawer" class="nav-drawer" aria-label="Navigation" @click.self="closeDrawer">
      <div class="nav-drawer-panel">
        <div class="nav-drawer-head">
          <NuxtLink to="/" class="brand"><span class="brand-mark"><BrandMark /></span><span>ORCHES</span></NuxtLink>
          <button class="icon-button" type="button" aria-label="Close navigation" @click="closeDrawer">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <MainNav variant="drawer" />
      </div>
    </dialog>
  </div>
</template>
