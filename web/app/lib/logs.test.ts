import { afterEach, describe, expect, it, vi } from 'vitest';
import { getProjects } from './api';
import { appendLogPage } from './logs';
import type { LogPage } from './types';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('API helpers', () => {
  it('includes HTTP status and response detail in errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'project not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );

    await expect(getProjects()).rejects.toThrow('HTTP 404: project not found');
  });

});

describe('log page appending', () => {
  it('appends new lines and caps the rendered window', () => {
    const page: LogPage = {
      run_id: 'run',
      invocation_id: 1,
      stream: 'stdout',
      lines: ['second', 'third'],
      next_offset: 12,
      eof: false,
    };

    expect(appendLogPage(['first', 'old'], page, 3)).toEqual(['old', 'second', 'third']);
  });
});
