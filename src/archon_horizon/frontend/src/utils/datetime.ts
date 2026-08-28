/**
 * Display timestamps in a fixed English locale so the UI does not flip to the
 * browser OS language (e.g. French month names on a fr_FR machine).
 *
 * Numbers (`toLocaleString` on counts) stay on the browser default; only calendar
 * labels are forced here.
 */
export const DISPLAY_LOCALE = 'en-US';

export function parseDate(value: string | undefined | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? null : date;
}

/** Time only, 24h — for dense log event rows. */
export function formatTime(value: string | undefined | null): string {
  const date = parseDate(value);
  if (!date) return value ? String(value) : '';
  return date.toLocaleTimeString(DISPLAY_LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** Compact date + time for session/run chips: "Aug 28, 15:20". */
export function formatChipDateTime(value: string | undefined | null): string {
  const date = parseDate(value);
  if (!date) return value ? String(value) : '';
  return date.toLocaleString(DISPLAY_LOCALE, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** Full date + time for detail panels. */
export function formatDateTime(value: string | undefined | null): string {
  const date = parseDate(value);
  if (!date) return value ? String(value) : '';
  return date.toLocaleString(DISPLAY_LOCALE, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** Alias used across the app for general timestamps. */
export function formatDate(value: string | undefined | null): string {
  return formatDateTime(value);
}

/** Day heading label: "Thu, Aug 28, 2026". */
export function formatDayLabel(value: string | undefined | null): string {
  const date = parseDate(value);
  if (!date) return 'Undated';
  return date.toLocaleDateString(DISPLAY_LOCALE, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

/** Local calendar day key YYYY-MM-DD for grouping. */
export function dayKey(value: string | undefined | null): string {
  const date = parseDate(value);
  if (!date) return 'undated';
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** Latest ISO-ish timestamp among candidates (by parsed instant). */
export function latestTimestamp(...values: Array<string | undefined | null>): string | undefined {
  let best: string | undefined;
  let bestMs = -Infinity;
  for (const value of values) {
    if (!value) continue;
    const ms = Date.parse(String(value));
    if (!Number.isFinite(ms)) continue;
    if (ms >= bestMs) {
      bestMs = ms;
      best = String(value);
    }
  }
  return best;
}
