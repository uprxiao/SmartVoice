import test from 'node:test';
import assert from 'node:assert/strict';
import { dateRangeError, lastShanghaiDays, shanghaiDate } from '../src/date-range.ts';

test('inclusive date range accepts one and 366 days, rejects reversed or 367 days', () => {
  assert.equal(dateRangeError('2024-01-01', '2024-01-01'), null);
  assert.equal(dateRangeError('2024-01-01', '2024-12-31'), null);
  assert.match(dateRangeError('2024-01-01', '2025-01-01'), /366/);
  assert.match(dateRangeError('2024-02-02', '2024-02-01'), /晚于/);
});

test('incomplete and normalized-invalid dates never reach the API', () => {
  for (const value of ['', '2024-2-1', '2023-02-29', '2024-02-30', 'not-a-date']) {
    assert.match(dateRangeError(value, '2024-12-31'), /有效/);
  }
});

test('Shanghai day boundary is independent of the browser local zone', () => {
  assert.equal(shanghaiDate(new Date('2026-10-17T15:59:59Z')), '2026-10-17');
  assert.equal(shanghaiDate(new Date('2026-10-17T16:00:00Z')), '2026-10-18');
  assert.deepEqual(lastShanghaiDays(7, new Date('2026-10-17T16:00:00Z')), { from: '2026-10-12', to: '2026-10-18' });
});
