import { describe, expect, it } from 'vitest';
import { hasDrift, profileDrift } from './profileDiff';
import type { PipelineProfile, PipelineStep } from './types';

const A = `node_${'a'.repeat(32)}`;
const B = `node_${'b'.repeat(32)}`;
const C = `node_${'c'.repeat(32)}`;

const step = (id: string, name: string, model: string, extra: Partial<PipelineStep> = {}): PipelineStep => ({
  id, name, model_selector: model, prompt: '', depends_on: [], position: { x: 0, y: 0 }, thinking: 'medium', ...extra,
});

const snapshot: PipelineProfile = {
  id: 'p', name: 'P', root_model: 'anthropic/root',
  steps: [step(A, 'A', 'anthropic/a'), step(B, 'B', 'anthropic/a', { depends_on: [A] })],
};

describe('profileDrift', () => {
  it('reports nothing for presentation-only edits', () => {
    const saved: PipelineProfile = {
      ...snapshot, name: 'P renamed',
      steps: [step(A, 'A moved', 'anthropic/a', { position: { x: 9, y: 9 } }), step(B, 'B', 'anthropic/a', { depends_on: [A] })],
    };
    expect(hasDrift(profileDrift(snapshot, saved))).toBe(false);
  });

  it('lists model, effort, prompt and dependency changes per node', () => {
    const saved: PipelineProfile = {
      ...snapshot,
      steps: [step(A, 'A', 'anthropic/b', { thinking: 'high' }), step(B, 'B', 'anthropic/a', { depends_on: [], prompt: 'x' })],
    };
    const drift = profileDrift(snapshot, saved);
    expect(drift.changed).toEqual([
      { id: A, name: 'A', from: 'anthropic/a', to: 'anthropic/b', fields: ['model', 'effort'] },
      { id: B, name: 'B', from: 'anthropic/a', to: 'anthropic/a', fields: ['prompt', 'dependencies'] },
    ]);
    expect(drift.root).toBeNull();
    expect(drift.differentProfile).toBe(false);
  });

  it('tracks root model, added and removed nodes, and a different profile', () => {
    const saved: PipelineProfile = { id: 'q', name: 'Q', root_model: '', steps: [step(A, 'A', 'anthropic/a'), step(C, 'C', 'x')] };
    const drift = profileDrift(snapshot, saved);
    expect(drift.differentProfile).toBe(true);
    expect(drift.root).toEqual({ from: 'anthropic/root', to: '' });
    expect(drift.added.map((item) => item.id)).toEqual([C]);
    expect(drift.removed.map((item) => item.id)).toEqual([B]);
    expect(drift.changed).toEqual([]);
    expect(hasDrift(drift)).toBe(true);
  });
});
