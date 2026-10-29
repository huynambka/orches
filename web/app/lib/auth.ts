/** Only a same-origin absolute path is an acceptable post-login target. */
export const safeNextPath = (raw: unknown): string => {
  if (typeof raw !== 'string' || !raw.startsWith('/')) return '/';
  if (raw.startsWith('//') || raw.includes('\\')) return '/';
  return raw;
};
