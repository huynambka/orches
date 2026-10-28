import type { LogPage } from './types';

export function appendLogPage(
  current: string[],
  page: LogPage,
  maxLines = 200,
): string[] {
  return [...current, ...page.lines].slice(-maxLines);
}
