import { describe, expect, it } from 'vitest';
import { exportFilename, exportProfile, parseProfileExport } from './profileTransfer';
import type { PipelineProfile } from './types';

const profile: PipelineProfile = {
  id: 'profile_abc',
  name: 'Review pipeline',
  revision: 7,
  root_prompt: 'Be careful',
  root_model: 'anthropic/x',
  root_thinking: 'high',
  pipeline_timeout_seconds: 7200,
  steps: [
    {
      id: 'node_' + 'a'.repeat(32), name: 'Writer', model_selector: 'm1', prompt: 'write',
      depends_on: [], position: { x: 10, y: 20 }, thinking: 'low', timeout_seconds: 900,
    },
    {
      id: 'node_' + 'b'.repeat(32), name: 'Reviewer', model_selector: 'm2', prompt: 'review',
      depends_on: ['node_' + 'a'.repeat(32)], position: { x: 300, y: 20 }, thinking: 'medium', timeout_seconds: 10800,
    },
  ],
};

describe('profile export/import', () => {
  it('round-trips the definition with fresh node ids and remapped dependencies', () => {
    const parsed = parseProfileExport(exportProfile(profile));
    expect(parsed.name).toBe('Review pipeline');
    expect(parsed.root_prompt).toBe('Be careful');
    expect(parsed.root_model).toBe('anthropic/x');
    expect(parsed.root_thinking).toBe('high');
    expect(parsed.pipeline_timeout_seconds).toBe(7200);
    expect(parsed.steps.map((s) => s.timeout_seconds)).toEqual([900, 10800]);
    expect(parsed.steps.map((s) => s.name)).toEqual(['Writer', 'Reviewer']);
    // ids regenerated, still valid node ids, dependency points at the new Writer id
    expect(parsed.steps[0]!.id).not.toBe(profile.steps[0]!.id);
    expect(parsed.steps[0]!.id).toMatch(/^node_[0-9a-f]{32}$/);
    expect(parsed.steps[1]!.depends_on).toEqual([parsed.steps[0]!.id]);
    // export carries no revision or profile id
    expect(exportProfile(profile)).not.toContain('profile_abc');
    expect(exportProfile(profile)).not.toContain('revision');
  });

  it('two imports of the same file produce disjoint node ids', () => {
    const file = exportProfile(profile);
    const first = parseProfileExport(file).steps.map((s) => s.id);
    const second = parseProfileExport(file).steps.map((s) => s.id);
    expect(first.some((id) => second.includes(id))).toBe(false);
  });

  it('rejects non-JSON, non-export and nameless files', () => {
    expect(() => parseProfileExport('nope')).toThrow('Not a JSON file.');
    expect(() => parseProfileExport('[1,2]')).toThrow('Expected a profile export object.');
    expect(() => parseProfileExport('{"name":"x","steps":[]}')).toThrow('orches-profile');
    expect(() => parseProfileExport('{"format":"orches-profile","name":" ","steps":[]}')).toThrow('no name');
    expect(() => parseProfileExport('{"format":"orches-profile","name":"x"}')).toThrow('steps array');
  });

  it('sanitizes hand-edited files: unknown deps dropped, bad effort defaulted', () => {
    const parsed = parseProfileExport(JSON.stringify({
      format: 'orches-profile', version: 1, name: 'Hand made', root_thinking: 'bogus', pipeline_timeout_seconds: 'fast',
      steps: [{ name: 'Solo', model_selector: 'm', prompt: '', depends_on: ['node_missing'], position: { x: 'NaN' }, thinking: 'turbo', timeout_seconds: 5 }],
    }));
    expect(parsed.root_thinking).toBe('');
    expect(parsed.steps[0]!.depends_on).toEqual([]);
    expect(parsed.steps[0]!.thinking).toBe('medium');
    expect(parsed.steps[0]!.position).toEqual({ x: 0, y: 0 });
    // below-minimum timeout falls back to the default; non-numeric pipeline timeout means "server default"
    expect(parsed.steps[0]!.timeout_seconds).toBe(10800);
    expect(parsed.pipeline_timeout_seconds).toBe(0);
  });

  it('builds a safe filename', () => {
    expect(exportFilename(profile)).toBe('Review pipeline.orches-profile.json');
    expect(exportFilename({ ...profile, name: 'a/b\\c:d?' })).toBe('a_b_c_d_.orches-profile.json');
  });
});
