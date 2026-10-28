import type { CostRow, CostUsage } from './types';

export interface ModelTotal extends CostUsage { model: string; }
export interface ProjectTotal extends CostUsage {
  project_id: string;
  project_name: string;
  models: ModelTotal[];
}

const EMPTY: CostUsage = {
  requests: 0, unpriced_requests: 0, cost_usd: null, input_tokens: null, output_tokens: null,
  cache_read_tokens: null, cache_write_tokens: null, total_tokens: null,
};

/** Null-preserving sum: null + null stays null (nothing reported), null + n = n. */
const addNullable = (a: number | null, b: number | null): number | null =>
  a === null ? b : b === null ? a : a + b;

/** Combine usage; a known cost next to unpriced requests remains a partial total. */
export function addUsage(a: CostUsage, b: CostUsage): CostUsage {
  return {
    requests: a.requests + b.requests,
    unpriced_requests: a.unpriced_requests + b.unpriced_requests,
    cost_usd: addNullable(a.cost_usd, b.cost_usd),
    input_tokens: addNullable(a.input_tokens, b.input_tokens),
    output_tokens: addNullable(a.output_tokens, b.output_tokens),
    cache_read_tokens: addNullable(a.cache_read_tokens, b.cache_read_tokens),
    cache_write_tokens: addNullable(a.cache_write_tokens, b.cache_write_tokens),
    total_tokens: addNullable(a.total_tokens, b.total_tokens),
  };
}

const pickUsage = (row: CostUsage): CostUsage => ({
  requests: row.requests, unpriced_requests: row.unpriced_requests, cost_usd: row.cost_usd,
  input_tokens: row.input_tokens, output_tokens: row.output_tokens,
  cache_read_tokens: row.cache_read_tokens, cache_write_tokens: row.cache_write_tokens,
  total_tokens: row.total_tokens,
});

/** Per-project totals with their model breakdown, in the server's project/model order. */
export function projectTotals(rows: CostRow[]): ProjectTotal[] {
  const result: ProjectTotal[] = [];
  const byId = new Map<string, ProjectTotal>();
  for (const row of rows) {
    let project = byId.get(row.project_id);
    if (!project) {
      project = { project_id: row.project_id, project_name: row.project_name, models: [], ...EMPTY };
      byId.set(row.project_id, project);
      result.push(project);
    }
    project.models.push({ model: row.model, ...pickUsage(row) });
    Object.assign(project, addUsage(project, row));
  }
  return result;
}

/** Totals per resolved model across every project, sorted by model name. */
export function modelTotals(rows: CostRow[]): ModelTotal[] {
  const byModel = new Map<string, ModelTotal>();
  for (const row of rows) {
    const current = byModel.get(row.model) ?? { model: row.model, ...EMPTY };
    byModel.set(row.model, { model: row.model, ...addUsage(current, row) });
  }
  return [...byModel.values()].sort((a, b) => a.model.localeCompare(b.model));
}

export function grandTotal(rows: CostUsage[]): CostUsage {
  return rows.reduce(addUsage, EMPTY);
}

/** USD for display: `—` when nothing was priced; tiny positives never round to a false zero. */
export function formatUsd(value: number | null): string {
  if (value === null) return '—';
  if (value === 0) return '$0.0000';
  if (value < 0.000001) return '<$0.000001';
  return `$${value.toFixed(6)}`;
}

export function formatTokens(value: number | null): string {
  return value === null ? '—' : value.toLocaleString();
}
