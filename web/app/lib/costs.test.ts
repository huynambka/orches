import { describe, expect, it } from 'vitest';
import { formatUsd, grandTotal, modelTotals, projectTotals } from './costs';
import type { CostRow } from './types';

const row = (project: string, model: string, cost: number | null, extra: Partial<CostRow> = {}): CostRow => ({
  project_id: project.toLowerCase(), project_name: project, model,
  requests: 1, unpriced_requests: cost === null ? 1 : 0, cost_usd: cost,
  input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: null, total_tokens: 15,
  ...extra,
});

const ROWS: CostRow[] = [
  row('P', 'anthropic/A', 0.003),
  row('P', 'anthropic/B', 0.002),
  row('P', 'm', null),
  row('Q', 'anthropic/A', 0.007),
];

describe('cost aggregation', () => {
  it('sums projects with their model breakdown and keeps unpriced counts', () => {
    const [p, q] = projectTotals(ROWS);
    expect(p?.project_name).toBe('P');
    expect(p?.cost_usd).toBeCloseTo(0.005, 10);
    expect(p?.requests).toBe(3);
    expect(p?.unpriced_requests).toBe(1);
    expect(p?.models.map((model) => model.model)).toEqual(['anthropic/A', 'anthropic/B', 'm']);
    expect(p?.input_tokens).toBe(30);
    expect(p?.cache_read_tokens).toBeNull();
    expect(q?.cost_usd).toBeCloseTo(0.007, 10);
    expect(q?.models).toHaveLength(1);
  });

  it('groups the same model across projects', () => {
    const totals = modelTotals(ROWS);
    expect(totals.map((total) => [total.model, total.cost_usd])).toEqual([
      ['anthropic/A', 0.01], ['anthropic/B', 0.002], ['m', null],
    ]);
    expect(totals[0]?.requests).toBe(2);
  });

  it('keeps a real zero distinct from nothing priced', () => {
    expect(grandTotal([row('P', 'a', 0)]).cost_usd).toBe(0);
    expect(grandTotal([row('P', 'a', null)]).cost_usd).toBeNull();
    expect(grandTotal([row('P', 'a', null), row('P', 'b', 0.5)]).cost_usd).toBe(0.5);
    expect(grandTotal([]).requests).toBe(0);
  });
});

describe('formatUsd', () => {
  it('renders unknown, zero, tiny and ordinary amounts distinctly', () => {
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd(0)).toBe('$0.0000');
    expect(formatUsd(0.0000004)).toBe('<$0.000001');
    expect(formatUsd(0.005)).toBe('$0.005000');
    expect(formatUsd(12.5)).toBe('$12.500000');
  });
});
