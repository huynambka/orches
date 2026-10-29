import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearToken, configureApiBaseUrl, getProjects, getToken, setUnauthorizedHandler } from './api';
import { safeNextPath } from './auth';

class FakeStorage {
  private items: Record<string, string> = {};
  getItem(key: string) { return this.items[key] ?? null; }
  setItem(key: string, value: string) { this.items[key] = value; }
  removeItem(key: string) { delete this.items[key]; }
}

beforeEach(() => {
  vi.stubGlobal('sessionStorage', new FakeStorage());
});

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
  configureApiBaseUrl('');
});

describe('configureApiBaseUrl', () => {
  it('accepts a bare https origin and http loopback for development', () => {
    expect(() => configureApiBaseUrl('https://api.example.com')).not.toThrow();
    expect(() => configureApiBaseUrl('http://127.0.0.1:8000')).not.toThrow();
    expect(() => configureApiBaseUrl('http://localhost:18600/')).not.toThrow();
  });

  it('rejects remote http, credentials, paths and queries', () => {
    for (const bad of [
      'http://api.example.com',
      'https://user:pw@api.example.com',
      'https://api.example.com/base',
      'https://api.example.com?x=1',
      'not-a-url',
    ]) {
      expect(() => configureApiBaseUrl(bad), bad).toThrow();
    }
  });
});

describe('401 handling', () => {
  it('clears the stored token and notifies the unauthorized handler', async () => {
    sessionStorage.setItem('orches-admin-token', 'stale-token');
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'authentication required' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(getProjects()).rejects.toThrow('HTTP 401: authentication required');
    expect(getToken()).toBeNull();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('sends the stored token as a bearer header', async () => {
    sessionStorage.setItem('orches-admin-token', 'live-token');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ projects: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getProjects();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/projects');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer live-token');
    clearToken();
  });
});

describe('safeNextPath', () => {
  it('allows only same-origin absolute paths', () => {
    expect(safeNextPath('/projects/abc?tab=1')).toBe('/projects/abc?tab=1');
    for (const bad of ['//evil.example', 'https://evil.example', 'projects', '/a\\b', 42, undefined]) {
      expect(safeNextPath(bad)).toBe('/');
    }
  });
});
