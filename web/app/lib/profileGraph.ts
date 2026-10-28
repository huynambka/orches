import { THINKING_LEVELS, type AgentPosition, type PipelineStep } from './types';

const byId = (steps: PipelineStep[]) => new Map(steps.map((step) => [step.id, step]));

const reachableFrom = (steps: PipelineStep[], start: string): Set<string> => {
  const outgoing = new Map<string, string[]>();
  for (const step of steps) {
    for (const source of step.depends_on) {
      const list = outgoing.get(source);
      if (list) list.push(step.id);
      else outgoing.set(source, [step.id]);
    }
  }
  const seen = new Set<string>();
  const stack = [start];
  while (stack.length) {
    const current = stack.pop()!;
    for (const next of outgoing.get(current) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    }
  }
  return seen;
};

export const canConnect = (steps: PipelineStep[], source: string, target: string): boolean =>
  connectionError(steps, source, target) === null;

export const connectSteps = (steps: PipelineStep[], source: string, target: string): PipelineStep[] => {
  if (!canConnect(steps, source, target)) return steps;
  return steps.map((step) =>
    step.id === target ? { ...step, depends_on: [...step.depends_on, source] } : step,
  );
};

export const removeStep = (steps: PipelineStep[], id: string): PipelineStep[] => {
  if (!steps.some((step) => step.id === id)) return steps;
  return steps
    .filter((step) => step.id !== id)
    .map((step) =>
      step.depends_on.includes(id)
        ? { ...step, depends_on: step.depends_on.filter((source) => source !== id) }
        : step,
    );
};

export const removeDependency = (steps: PipelineStep[], source: string, target: string): PipelineStep[] => {
  const targetStep = steps.find((step) => step.id === target);
  if (!targetStep || !targetStep.depends_on.includes(source)) return steps;
  return steps.map((step) =>
    step.id === target
      ? { ...step, depends_on: step.depends_on.filter((dependency) => dependency !== source) }
      : step,
  );
};

/**
 * Splice a brand-new step into an existing edge: `source → insert → target`.
 * `source` may be `'root'`, covering the implicit edge to a dependency-free
 * target. Returns the same reference when the edge or ids are not as drawn.
 */
export const insertStepBetween = (
  steps: PipelineStep[],
  source: string,
  target: string,
  insert: PipelineStep,
): PipelineStep[] => {
  const targetStep = steps.find((step) => step.id === target);
  if (!targetStep || steps.some((step) => step.id === insert.id)) return steps;
  if (source === 'root') {
    if (targetStep.depends_on.length > 0) return steps;
    return [
      ...steps.map((step) => (step.id === target ? { ...step, depends_on: [insert.id] } : step)),
      { ...insert, depends_on: [] },
    ];
  }
  if (!steps.some((step) => step.id === source) || !targetStep.depends_on.includes(source)) return steps;
  return [
    ...steps.map((step) =>
      step.id === target
        ? { ...step, depends_on: step.depends_on.map((dependency) => (dependency === source ? insert.id : dependency)) }
        : step,
    ),
    { ...insert, depends_on: [source] },
  ];
};

/** Human-readable reason a source→target dependency is not allowed; null when valid. */
export const connectionError = (steps: PipelineStep[], source: string, target: string): string | null => {
  if (source === target) return 'An agent cannot depend on itself.';
  const nodes = byId(steps);
  if (!nodes.has(source) || !nodes.has(target)) return 'Unknown agent.';
  if (nodes.get(target)!.depends_on.includes(source)) return 'That dependency already exists.';
  if (reachableFrom(steps, target).has(source)) return 'That connection would create a cycle.';
  return null;
};

export type AnchorSide = 'left' | 'right' | 'top' | 'bottom';

/**
 * Facing edge anchors from relative node positions: horizontal handles when the
 * displacement is mostly horizontal (|dx| >= |dy|), vertical handles otherwise.
 */
export const anchorSides = (
  source: AgentPosition,
  target: AgentPosition,
): { source: AnchorSide; target: AnchorSide } => {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0 ? { source: 'right', target: 'left' } : { source: 'left', target: 'right' };
  }
  return dy >= 0 ? { source: 'bottom', target: 'top' } : { source: 'top', target: 'bottom' };
};

export const POSITION_BOUND = 10000;
const COLUMN_GAP = 280;
const ROW_GAP = 150;
const ORIGIN = { x: 80, y: 80 };

/**
 * Deterministic auto-layout: column = longest dependency chain depth,
 * row = original step order within that column, clamped to the backend bound.
 */
export const layoutProfileSteps = (steps: PipelineStep[]): PipelineStep[] => {
  const nodes = byId(steps);
  const depths = new Map<string, number>();
  const depthOf = (id: string, trail: Set<string>): number => {
    const known = depths.get(id);
    if (known !== undefined) return known;
    if (trail.has(id)) return 0; // defensive: cycles are rejected elsewhere
    trail.add(id);
    const step = nodes.get(id);
    const depth = step && step.depends_on.length
      ? 1 + Math.max(...step.depends_on.map((dep) => depthOf(dep, trail)))
      : 0;
    trail.delete(id);
    depths.set(id, depth);
    return depth;
  };
  const rows = new Map<number, number>();
  const clamp = (value: number) => Math.min(POSITION_BOUND, Math.max(-POSITION_BOUND, value));
  return steps.map((step) => {
    const column = depthOf(step.id, new Set());
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    return {
      ...step,
      position: { x: clamp(ORIGIN.x + column * COLUMN_GAP), y: clamp(ORIGIN.y + row * ROW_GAP) },
    };
  });
};

const MAX_NODES = 64;
const MAX_PROMPT = 10000;
const MAX_NAME = 160;
const MAX_MODEL = 256;

/** Client-side mirror of the backend profile limits; the backend stays authoritative. */
export const validateProfileDraft = (
  name: string,
  steps: PipelineStep[],
  rootPrompt: string,
): string[] => {
  const problems: string[] = [];
  if (!name.trim()) problems.push('Profile name is required.');
  if (name.length > MAX_NAME) problems.push(`Profile name must be at most ${MAX_NAME} characters.`);
  if (rootPrompt.length > MAX_PROMPT) problems.push(`Orchestrator prompt must be at most ${MAX_PROMPT} characters.`);
  if (steps.length === 0) problems.push('Add at least one agent before this profile can run.');
  if (steps.length > MAX_NODES) problems.push(`Profiles may contain at most ${MAX_NODES} agents.`);
  for (const step of steps) {
    const label = step.name.trim() || step.id;
    if (!step.name.trim()) problems.push(`An agent is missing a name (${step.id.slice(0, 13)}…).`);
    if (step.name.length > MAX_NAME) problems.push(`Agent "${label}" name must be at most ${MAX_NAME} characters.`);
    if (!step.model_selector.trim()) problems.push(`Agent "${label}" has no model selected.`);
    if (step.model_selector.length > MAX_MODEL) problems.push(`Agent "${label}" model selector is too long.`);
    if (step.prompt.length > MAX_PROMPT) problems.push(`Agent "${label}" prompt must be at most ${MAX_PROMPT} characters.`);
    if (!(THINKING_LEVELS as readonly string[]).includes(step.thinking)) problems.push(`Agent "${label}" has an invalid effort level.`);
    if (!Number.isFinite(step.position.x) || !Number.isFinite(step.position.y)
      || Math.abs(step.position.x) > POSITION_BOUND || Math.abs(step.position.y) > POSITION_BOUND) {
      problems.push(`Agent "${label}" position is out of bounds.`);
    }
  }
  return problems;
};
