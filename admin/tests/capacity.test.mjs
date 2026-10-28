import test from 'node:test';
import assert from 'node:assert/strict';
import { capacityInput, percentileLabel, saveCapacity, validationSummary } from '../src/capacity.ts';
import { ApiError } from '../src/http.ts';

const report = {
  report_id: 'fixture-report', tested_at: '2026-10-18T00:00:00Z', target_sessions: 20,
  environment_fingerprint: 'fixture', app_commit: 'fixture', config_sha256: 'a'.repeat(64),
  fixture_manifest_sha256: 'b'.repeat(64), hardware_model_manifest_sha256: 'c'.repeat(64),
  evidence_sha256: 'd'.repeat(64), normal_total: 1600, normal_completed: 1600,
  rejected: 0, failed: 0, fallback: 0, stop_to_result_p50_ms: 1000, stop_to_result_p95_ms: 2000,
  desktop_stop_to_readback_p50_ms: 1100, desktop_stop_to_readback_p95_ms: 2100, soak_passed: true,
};
const view = (version = 1, value = 20) => ({
  config: { version, max_active_sessions: value, updated_at: '2026-10-18T00:00:00Z', updated_by: 'fixture-admin' },
  deployment: { admission_ceiling: 100, effective_ceiling: 100, active_pcm_budget_bytes: 128 * 1024 ** 2,
    retry_pcm_budget_bytes: 32 * 1024 ** 2, asr_execution_slots: 2, llm_max_inflight: 2 },
  runtime: null,
  validation: { status: 'unverified', report: null },
});

test('capacity input accepts deployment-defined limits without a hard-coded 20', () => {
  assert.equal(capacityInput('50', 100), 50);
  assert.equal(capacityInput('100', 100), 100);
  assert.equal(capacityInput(' 1 ', 20), 1);
  for (const invalid of ['', '0', '-1', '1.5', '1e1', 'true', '101', 'Infinity', '2147483648']) {
    assert.throws(() => capacityInput(invalid, 100));
  }
});

test('unknown, unverified, valid and stale reports retain distinct meanings', () => {
  assert.equal(validationSummary(null).label, '未知');
  assert.equal(validationSummary(view()).label, '未验证');
  const current = view();
  current.validation = { status: 'valid', report };
  assert.equal(validationSummary(current).label, '已验证');
  assert.equal(validationSummary(current).exceeds, false);
  current.config.max_active_sessions = 50;
  assert.equal(validationSummary(current).exceeds, true);
  current.validation.status = 'stale';
  assert.equal(validationSummary(current).label, '报告已过期');
  current.validation.report = null;
  assert.equal(validationSummary(current).label, '未验证');
});

test('missing metrics, no samples, failed percentiles and real zero are different', () => {
  assert.equal(percentileLabel(null, 'p95'), '未知');
  const runtime = { sample_count: 0, failed_count: 0, stop_to_result_p50_ms: null, stop_to_result_p95_ms: null };
  assert.equal(percentileLabel(runtime, 'p95'), '无样本');
  runtime.sample_count = 4;
  runtime.failed_count = 1;
  assert.match(percentileLabel(runtime, 'p95'), /含失败/);
  runtime.stop_to_result_p50_ms = 0;
  assert.equal(percentileLabel(runtime, 'p50'), '0 ms');
});

test('normal save sends the exact CAS version and desired value once', async () => {
  const calls = [];
  const result = await saveCapacity({
    updateCapacity: async (...args) => { calls.push(['PATCH', ...args]); return view(2, 10); },
    capacity: async () => { calls.push(['GET']); return view(2, 10); },
  }, 1, 10);
  assert.deepEqual(calls, [['PATCH', 1, 10]]);
  assert.equal(result.kind, 'saved');
});

test('lost mutation response confirms effective state by GET without re-sending PATCH', async () => {
  const calls = [];
  const result = await saveCapacity({
    updateCapacity: async (...args) => { calls.push(['PATCH', ...args]); throw new ApiError(0, 'REQUEST_TIMEOUT', 'timeout'); },
    capacity: async () => { calls.push(['GET']); return view(2, 10); },
  }, 1, 10);
  assert.deepEqual(calls, [['PATCH', 1, 10], ['GET']]);
  assert.equal(result.kind, 'confirmed');
  assert.match(result.message, /保存响应未确认/);
});

test('CAS conflict remains a review even when another admin chose the same value', async () => {
  let patches = 0;
  const result = await saveCapacity({
    updateCapacity: async () => { patches += 1; throw new ApiError(409, 'VERSION_CONFLICT', 'conflict'); },
    capacity: async () => view(3, 10),
  }, 1, 10);
  assert.equal(patches, 1);
  assert.equal(result.kind, 'review');
  assert.equal(result.view.config.version, 3);
  assert.match(result.message, /输入已保留/);
});

test('failed reconciliation never claims success or retries the mutation', async () => {
  let patches = 0;
  const result = await saveCapacity({
    updateCapacity: async () => { patches += 1; throw new ApiError(503, 'SERVICE_UNAVAILABLE', 'offline'); },
    capacity: async () => { throw new ApiError(0, 'NETWORK_ERROR', 'offline'); },
  }, 1, 10);
  assert.equal(result.kind, 'review');
  assert.equal(result.view, null);
  assert.equal(patches, 1);
});

test('readback showing an unchanged old value requires review, not success', async () => {
  const result = await saveCapacity({
    updateCapacity: async () => { throw new ApiError(0, 'NETWORK_ERROR', 'offline'); },
    capacity: async () => view(1, 20),
  }, 1, 10);
  assert.equal(result.kind, 'review');
  assert.equal(result.view.config.max_active_sessions, 20);
});

test('authorization and validation errors do not trigger recovery reads', async () => {
  for (const status of [401, 403, 422]) {
    const error = new ApiError(status, 'TEST', 'fixture');
    await assert.rejects(saveCapacity({
      updateCapacity: async () => { throw error; },
      capacity: async () => assert.fail('must not read after a definite rejection'),
    }, 1, 10), (actual) => actual === error);
  }
});

test('authorization failure during readback reaches the account boundary', async () => {
  await assert.rejects(saveCapacity({
    updateCapacity: async () => { throw new ApiError(0, 'REQUEST_TIMEOUT', 'timeout'); },
    capacity: async () => { throw new ApiError(403, 'FORBIDDEN', 'revoked'); },
  }, 1, 10), (error) => error.status === 403);
});
