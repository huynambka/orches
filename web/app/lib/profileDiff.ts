import type { PipelineProfile, PipelineStep } from './types';

export interface StepChange {
  id: string;
  name: string;
  /** Model as run → model now saved (equal when only prompt/effort/dependencies changed). */
  from: string;
  to: string;
  fields: Array<'model' | 'prompt' | 'effort' | 'dependencies'>;
}

export interface ProfileDrift {
  /** The project now selects a different profile than the run executed. */
  differentProfile: boolean;
  root: { from: string; to: string } | null;
  changed: StepChange[];
  added: PipelineStep[];
  removed: PipelineStep[];
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && new Set(a).size === new Set([...a, ...b]).size;

/**
 * What differs between the profile a run executed (its snapshot) and the profile the
 * project now has saved. Mirrors the backend's reuse rule: a node whose model, prompt,
 * effort or dependencies changed executes again on Resume.
 */
export function profileDrift(snapshot: PipelineProfile, saved: PipelineProfile): ProfileDrift {
  const previous = new Map(snapshot.steps.map((step) => [step.id, step]));
  const current = new Map(saved.steps.map((step) => [step.id, step]));
  const changed: StepChange[] = [];
  for (const step of saved.steps) {
    const before = previous.get(step.id);
    if (!before) continue;
    const fields: StepChange['fields'] = [];
    if (before.model_selector !== step.model_selector) fields.push('model');
    if (before.prompt !== step.prompt) fields.push('prompt');
    if ((before.thinking || 'medium') !== (step.thinking || 'medium')) fields.push('effort');
    if (!sameSet(before.depends_on, step.depends_on)) fields.push('dependencies');
    if (fields.length) changed.push({ id: step.id, name: step.name, from: before.model_selector, to: step.model_selector, fields });
  }
  const rootFrom = snapshot.root_model || '';
  const rootTo = saved.root_model || '';
  return {
    differentProfile: snapshot.id !== saved.id,
    root: rootFrom !== rootTo ? { from: rootFrom, to: rootTo } : null,
    changed,
    added: saved.steps.filter((step) => !previous.has(step.id)),
    removed: snapshot.steps.filter((step) => !current.has(step.id)),
  };
}

export const hasDrift = (drift: ProfileDrift) =>
  drift.differentProfile || drift.root !== null || drift.changed.length > 0 || drift.added.length > 0 || drift.removed.length > 0;
