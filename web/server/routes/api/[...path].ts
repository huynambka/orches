import { defineEventHandler, getRequestURL, proxyRequest, createError } from 'h3';

const ALLOWED_HOSTS: Record<string, true> = { '127.0.0.1': true, localhost: true, '[::1]': true };

/**
 * Validate the configured upstream so this route can never become an open proxy:
 * http only, a loopback host, and no credentials/path/query/hash.
 */
function resolveBase(raw: string): URL {
  let base: URL;
  try {
    base = new URL(raw);
  } catch {
    throw createError({ statusCode: 500, statusMessage: 'Invalid API proxy URL' });
  }
  const loopback = ALLOWED_HOSTS[base.hostname] || ALLOWED_HOSTS[base.host];
  if (
    base.protocol !== 'http:' ||
    !loopback ||
    base.username ||
    base.password ||
    (base.pathname && base.pathname !== '/') ||
    base.search ||
    base.hash
  ) {
    throw createError({ statusCode: 500, statusMessage: 'API proxy URL must be a bare http loopback origin' });
  }
  return base;
}

export default defineEventHandler((event) => {
  const base = resolveBase(useRuntimeConfig(event).apiProxyUrl);
  const incoming = getRequestURL(event);
  const target = new URL(incoming.pathname + incoming.search, base);
  return proxyRequest(event, target.toString(), {
    // Forward only the headers the backend needs; strip hop/auth leakage from the browser.
    fetchOptions: { redirect: 'manual' },
  });
});
