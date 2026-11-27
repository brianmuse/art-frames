import sample from '../../public/dayframe.json';

export function pageContext(locals: Record<string, unknown>) {
  const serverRendered = Boolean(locals.snapshot);
  const snapshot = (locals.snapshot || sample) as Record<string, any>;
  const now = new Date(snapshot.now);
  const timezone = snapshot.timezone || 'UTC';
  const format = (options: Intl.DateTimeFormatOptions) => now.toLocaleDateString('en-US', { timeZone: timezone, ...options });
  return { snapshot, serverRendered, format };
}
