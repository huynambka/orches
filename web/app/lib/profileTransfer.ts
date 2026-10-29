import {
  DEFAULT_THINKING, THINKING_LEVELS,
  type PipelineProfile, type PipelineStep, type ThinkingLevel,
} from './types';

/** Portable profile file: definition only — no ids, revisions or project state. */
export interface ProfileExport {
  format: 'orches-profile';
  version: 1;
  name: string;
  root_prompt: string;
  root_model: string;
  root_thinking: string;
  steps: PipelineStep[];
}

const MAX_STEPS = 64;

export const exportProfile = (profile: PipelineProfile): string =>
  JSON.stringify(
    {
      format: 'orches-profile',
      version: 1,
      name: profile.name || profile.id,
      root_prompt: profile.root_prompt || '',
      root_model: profile.root_model || '',
      root_thinking: profile.root_thinking || '',
      steps: profile.steps.map((step) => ({
        id: step.id,
        name: step.name,
        model_selector: step.model_selector,
        prompt: step.prompt,
        depends_on: [...step.depends_on],
        position: { x: step.position.x, y: step.position.y },
        thinking: step.thinking,
      })),
    } satisfies ProfileExport,
    null,
    2,
  );

export const exportFilename = (profile: PipelineProfile): string => {
  const base = (profile.name || profile.id).replaceAll(/[^A-Za-z0-9._ -]+/g, '_').trim() || 'profile';
  return `${base}.orches-profile.json`;
};

const newNodeId = (): string =>
  `node_${crypto.randomUUID().replaceAll('-', '')}`;

const text = (value: unknown): string => (typeof value === 'string' ? value : '');

const finite = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

/**
 * Parse an exported profile file into a fresh definition. Node ids are
 * regenerated (dependencies remapped) so the same file can be imported many
 * times without colliding with existing profiles. Field-level limits are
 * enforced by the backend validator on save.
 */
export const parseProfileExport = (raw: string): Omit<ProfileExport, 'format' | 'version'> => {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new Error('Not a JSON file.');
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new Error('Expected a profile export object.');
  }
  const record = data as Record<string, unknown>;
  if (record.format !== 'orches-profile') {
    throw new Error('Not an Orches profile export (missing "format": "orches-profile").');
  }
  const name = text(record.name).trim();
  if (!name) throw new Error('Profile export has no name.');
  if (!Array.isArray(record.steps)) throw new Error('Profile export has no steps array.');
  if (record.steps.length > MAX_STEPS) throw new Error(`Profile export has more than ${MAX_STEPS} agents.`);

  const idMap: Record<string, string> = {};
  for (const item of record.steps) {
    const oldId = text((item as Record<string, unknown> | null)?.id);
    if (oldId) idMap[oldId] = newNodeId();
  }
  const steps: PipelineStep[] = record.steps.map((item, index) => {
    if (typeof item !== 'object' || item === null) throw new Error(`Agent #${index + 1} is not an object.`);
    const step = item as Record<string, unknown>;
    const position = (typeof step.position === 'object' && step.position !== null
      ? step.position : {}) as Record<string, unknown>;
    const thinking = text(step.thinking);
    const dependsOn = Array.isArray(step.depends_on) ? step.depends_on : [];
    return {
      id: idMap[text(step.id)] ?? newNodeId(),
      name: text(step.name).trim() || `Agent ${index + 1}`,
      model_selector: text(step.model_selector),
      prompt: text(step.prompt),
      // Unknown dependencies (hand-edited files) are dropped instead of failing the save.
      depends_on: dependsOn.map((dep) => idMap[text(dep)]).filter((dep): dep is string => Boolean(dep)),
      position: { x: finite(position.x), y: finite(position.y) },
      thinking: ((THINKING_LEVELS as readonly string[]).includes(thinking) ? thinking : DEFAULT_THINKING) as ThinkingLevel,
    };
  });
  const rootThinking = text(record.root_thinking);
  return {
    name,
    root_prompt: text(record.root_prompt),
    root_model: text(record.root_model),
    root_thinking: (THINKING_LEVELS as readonly string[]).includes(rootThinking) ? rootThinking : '',
    steps,
  };
};
