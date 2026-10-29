import { describe, expect, it } from 'vitest';
import { anchorSides, canConnect, connectSteps, connectionError, insertStepBetween, layoutProfileSteps, removeDependency, removeStep, validateProfileDraft } from './profileGraph';
import type { PipelineStep } from './types';

const step = (id: string, depends_on: string[] = [], x = 0, y = 0): PipelineStep => ({
  id,
  name: id.toUpperCase(),
  model_selector: `model-${id}`,
  prompt: `prompt-${id}`,
  depends_on,
  position: { x, y },
  thinking: 'medium',
  timeout_seconds: 10800,
});

describe('profile graph edits', () => {
  it('connects distinct workers and stores the dependency on the target', () => {
    const next = connectSteps([step('a'), step('b')], 'a', 'b');
    expect(next.find((node) => node.id === 'b')?.depends_on).toEqual(['a']);
    expect(next.find((node) => node.id === 'a')?.depends_on).toEqual([]);
  });

  it('rejects self, duplicate, unknown and cycle-closing connections', () => {
    const graph = [step('a'), step('b', ['a'])];
    expect(canConnect(graph, 'a', 'a')).toBe(false);
    expect(canConnect(graph, 'a', 'b')).toBe(false); // duplicate a→b
    expect(canConnect(graph, 'a', 'missing')).toBe(false);
    expect(canConnect(graph, 'b', 'a')).toBe(false); // would close a→b→a cycle
    expect(connectSteps(graph, 'b', 'a')).toBe(graph);
  });

  it('allows a second independent incoming dependency', () => {
    const graph = [step('a'), step('b', ['a']), step('c')];
    expect(canConnect(graph, 'c', 'b')).toBe(true);
    expect(connectSteps(graph, 'c', 'b').find((node) => node.id === 'b')?.depends_on).toEqual(['a', 'c']);
  });

  it('removes a worker and strips it from every remaining dependency, preserving others', () => {
    const graph = [step('a'), step('b', ['a'], 40, 60), step('c', ['a', 'b'])];
    const next = removeStep(graph, 'a');
    expect(next.map((node) => node.id)).toEqual(['b', 'c']);
    expect(next.find((node) => node.id === 'b')).toEqual(step('b', [], 40, 60));
    expect(next.find((node) => node.id === 'c')?.depends_on).toEqual(['b']);
  });

  it('removing the last incoming dependency leaves the node root-eligible', () => {
    const graph = [step('a'), step('b', ['a'])];
    const next = removeDependency(graph, 'a', 'b');
    expect(next.find((node) => node.id === 'b')?.depends_on).toEqual([]);
  });

  it('returns the same reference for no-op operations', () => {
    const graph = [step('a'), step('b', ['a'])];
    expect(removeStep(graph, 'missing')).toBe(graph);
    expect(removeDependency(graph, 'missing', 'b')).toBe(graph);
    expect(removeDependency(graph, 'a', 'missing')).toBe(graph);
    expect(connectSteps(graph, 'a', 'b')).toBe(graph);
  });
});

describe('insert step on an edge', () => {
  it('splices into a dependency edge, replacing only that dependency', () => {
    const steps = [step('a'), step('c'), step('b', ['a', 'c'])];
    const result = insertStepBetween(steps, 'a', 'b', step('n'));
    expect(result.find((s) => s.id === 'b')?.depends_on).toEqual(['n', 'c']);
    expect(result.find((s) => s.id === 'n')?.depends_on).toEqual(['a']);
    expect(result).toHaveLength(4);
  });

  it('splices into a root edge, making the new step the sole dependency', () => {
    const steps = [step('b')];
    const result = insertStepBetween(steps, 'root', 'b', step('n'));
    expect(result.find((s) => s.id === 'b')?.depends_on).toEqual(['n']);
    expect(result.find((s) => s.id === 'n')?.depends_on).toEqual([]);
  });

  it('is a same-reference no-op for edges that are not drawn', () => {
    const steps = [step('a'), step('b', ['a'])];
    expect(insertStepBetween(steps, 'b', 'a', step('n'))).toBe(steps);
    expect(insertStepBetween(steps, 'root', 'b', step('n'))).toBe(steps);
    expect(insertStepBetween(steps, 'a', 'missing', step('n'))).toBe(steps);
    expect(insertStepBetween(steps, 'a', 'b', step('a'))).toBe(steps);
  });
});

describe('connection error reasons', () => {
  it('names the specific rule that blocks a connection', () => {
    const graph = [step('a'), step('b', ['a'])];
    expect(connectionError(graph, 'a', 'a')).toMatch(/itself/);
    expect(connectionError(graph, 'a', 'b')).toMatch(/already exists/);
    expect(connectionError(graph, 'b', 'a')).toMatch(/cycle/);
    expect(connectionError(graph, 'a', 'missing')).toMatch(/Unknown/);
    expect(connectionError(graph, 'b', 'a')).not.toBeNull();
    expect(connectionError([step('a'), step('b')], 'a', 'b')).toBeNull();
  });
});

describe('auto layout', () => {
  it('columns follow the longest dependency chain and rows keep step order', () => {
    const graph = [
      step('a'), step('b'), step('c', ['a', 'b']), step('d', ['c']), step('e', ['a']),
    ];
    const laid = layoutProfileSteps(graph);
    const byId = Object.fromEntries(laid.map((item) => [item.id, item.position]));
    expect(byId.a).toEqual({ x: 80, y: 80 });
    expect(byId.b).toEqual({ x: 80, y: 230 });
    expect(byId.c).toEqual({ x: 360, y: 80 });
    expect(byId.d).toEqual({ x: 640, y: 80 });
    expect(byId.e).toEqual({ x: 360, y: 230 });
  });

  it('is deterministic and stays within position bounds', () => {
    const chain = Array.from({ length: 64 }, (_, index) =>
      step(`n${index}`, index ? [`n${index - 1}`] : []));
    const laid = layoutProfileSteps(chain);
    expect(layoutProfileSteps(chain)).toEqual(laid);
    for (const item of laid) {
      expect(Math.abs(item.position.x)).toBeLessThanOrEqual(10000);
      expect(Math.abs(item.position.y)).toBeLessThanOrEqual(10000);
    }
    expect(laid[63]!.position.x).toBe(10000); // 80 + 63*280 clamps to the backend bound
  });
});

describe('draft validation', () => {
  it('mirrors the backend limits that block a save', () => {
    expect(validateProfileDraft('Pipeline', [step('a')], 'hint')).toEqual([]);
    expect(validateProfileDraft('  ', [step('a')], '')).toContainEqual(expect.stringMatching(/name is required/));
    expect(validateProfileDraft('P', [], '')).toContainEqual(expect.stringMatching(/at least one agent/));
    const unnamed = { ...step('a'), name: ' ' };
    expect(validateProfileDraft('P', [unnamed], '')).toContainEqual(expect.stringMatching(/missing a name/));
    const modelless = { ...step('a'), model_selector: '' };
    expect(validateProfileDraft('P', [modelless], '')).toContainEqual(expect.stringMatching(/no model/));
    const longPrompt = { ...step('a'), prompt: 'x'.repeat(10001) };
    expect(validateProfileDraft('P', [longPrompt], '')).toContainEqual(expect.stringMatching(/prompt must be at most/));
    expect(validateProfileDraft('P', [step('a')], 'y'.repeat(10001))).toContainEqual(expect.stringMatching(/Orchestrator prompt/));
    const far = { ...step('a'), position: { x: 10001, y: 0 } };
    expect(validateProfileDraft('P', [far], '')).toContainEqual(expect.stringMatching(/out of bounds/));
    const effort = { ...step('a'), thinking: 'turbo' as PipelineStep['thinking'] };
    expect(validateProfileDraft('P', [effort], '')).toContainEqual(expect.stringMatching(/invalid effort/));
  });
});

describe('dynamic edge anchors', () => {
  it('faces the connected node: horizontal wins at |dx| >= |dy|, vertical otherwise', () => {
    expect(anchorSides({ x: 0, y: 0 }, { x: 200, y: 50 })).toEqual({ source: 'right', target: 'left' });
    expect(anchorSides({ x: 200, y: 50 }, { x: 0, y: 0 })).toEqual({ source: 'left', target: 'right' });
    expect(anchorSides({ x: 0, y: 0 }, { x: 50, y: 200 })).toEqual({ source: 'bottom', target: 'top' });
    expect(anchorSides({ x: 50, y: 200 }, { x: 0, y: 0 })).toEqual({ source: 'top', target: 'bottom' });
    // Exact diagonal and identical positions stay horizontal for stable layouts.
    expect(anchorSides({ x: 0, y: 0 }, { x: 100, y: 100 })).toEqual({ source: 'right', target: 'left' });
    expect(anchorSides({ x: 0, y: 0 }, { x: 0, y: 0 })).toEqual({ source: 'right', target: 'left' });
  });
});
