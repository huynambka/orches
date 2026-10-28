<script setup lang="ts">
const props = withDefaults(defineProps<{ variant: 'sidebar' | 'drawer'; collapsed?: boolean }>(), {
  collapsed: false,
});

const gridIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>`;
const serverIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>`;
const flowIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="m7.5 11 9-4M7.5 13l9 4"/></svg>`;

const LINKS = [
  { href: '/', label: 'Projects', icon: gridIcon },
  { href: '/profiles', label: 'Profiles', icon: flowIcon },
  { href: '/connections', label: 'Connections', icon: serverIcon },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === '/connections') return pathname.startsWith('/connections');
  if (href === '/profiles') return pathname.startsWith('/profiles');
  return pathname === '/' || pathname.startsWith('/projects');
}

const route = useRoute();
const active = (href: string) => isActive(route.path || '/', href);
</script>

<template>
  <nav :aria-label="props.variant === 'drawer' ? 'Mobile navigation' : 'Main navigation'">
    <NuxtLink
      v-for="link in LINKS"
      :key="link.href"
      :to="link.href"
      :class="`nav-item ${active(link.href) ? 'active' : ''}`"
      :aria-current="active(link.href) ? 'page' : undefined"
      :title="props.collapsed ? link.label : undefined"
    >
      <!-- eslint-disable-next-line vue/no-v-html -->
      <span class="nav-icon" v-html="link.icon" /><!-- static trusted icon markup -->
      <span class="nav-text">{{ link.label }}</span>
      <span v-if="props.collapsed" class="sr-only">{{ link.label }}</span>
    </NuxtLink>
  </nav>
</template>
