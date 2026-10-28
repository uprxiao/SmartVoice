import type { Session } from '@shared/types';

/** Invalid or reached finite deadlines never authorize a cached body read. */
export function historyContentExpired(item: Session, now = Date.now()): boolean {
  if (item.content_expired) return true;
  if (item.content_expires_at === null) return false;
  const deadline = Date.parse(item.content_expires_at);
  return !Number.isFinite(deadline) || deadline <= now;
}
