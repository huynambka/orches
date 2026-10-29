// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-01-01',
  devtools: { enabled: false },
  ssr: true,
  runtimeConfig: {
    // Overridable with NUXT_API_PROXY_URL; the server route validates it per request.
    apiProxyUrl: 'http://127.0.0.1:8000',
    public: {
      // Direct browser->backend origin (NUXT_PUBLIC_API_BASE_URL). Empty keeps
      // the same-origin /api proxy for local development.
      apiBaseUrl: '',
    },
  },
  css: [
    '@vue-flow/core/dist/style.css',
    '@vue-flow/core/dist/theme-default.css',
    '@vue-flow/controls/dist/style.css',
    '@vue-flow/minimap/dist/style.css',
    '~/assets/css/main.css',
  ],
  app: {
    head: {
      title: 'Orches Monitor',
      meta: [{ name: 'description', content: 'Local Orches agent pipeline client' }],
      htmlAttrs: { lang: 'en' },
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
        { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' },
        {
          rel: 'stylesheet',
          href: 'https://fonts.googleapis.com/css2?family=Google+Sans+Flex:wght@100..1000&family=Google+Sans+Code:wght@300..800&display=swap',
        },
      ],
    },
  },
});
