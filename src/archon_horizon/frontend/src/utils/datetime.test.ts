import { describe, expect, it } from 'vitest';
import {
  dayKey,
  formatChipDateTime,
  formatDateTime,
  formatDayLabel,
  formatTime,
  latestTimestamp,
} from './datetime';

describe('datetime display helpers', () => {
  it('formats in fixed English locale (not browser OS language)', () => {
    // A mid-month UTC instant → English month abbreviation regardless of host locale.
    const iso = '2026-08-28T07:20:00.000Z';
    expect(formatDayLabel(iso)).toMatch(/Aug/);
    expect(formatDayLabel(iso)).not.toMatch(/août|Août|août/i);
    expect(formatChipDateTime(iso)).toMatch(/Aug/);
    expect(formatDateTime(iso)).toMatch(/Aug/);
  });

  it('includes date and time on chip labels', () => {
    const chip = formatChipDateTime('2026-08-28T15:20:00.000Z');
    // "Aug 28, 15:20" (local offset may shift the hour; month+day must remain).
    expect(chip).toMatch(/Aug/);
    expect(chip).toMatch(/28/);
    expect(chip).toMatch(/\d{1,2}:\d{2}/);
  });

  it('formatTime is time-only for dense log rows', () => {
    const t = formatTime('2026-08-28T15:20:33.000Z');
    expect(t).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it('latestTimestamp picks the newest ISO instant', () => {
    expect(
      latestTimestamp(
        '2026-08-01T00:00:00Z',
        '2026-08-28T12:00:00Z',
        undefined,
        '2026-08-10T00:00:00Z',
      ),
    ).toBe('2026-08-28T12:00:00Z');
  });

  it('dayKey is a local YYYY-MM-DD', () => {
    expect(dayKey('not-a-date')).toBe('undated');
    expect(dayKey('2026-08-28T12:00:00.000Z')).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});
