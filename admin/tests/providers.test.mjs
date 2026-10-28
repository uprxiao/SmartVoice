import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createPayload, emptyForm, formError, needsCloudConfirmation, patchPayload, providerErrorMessage, rowStatus,
} from '../src/providers.ts';
import { ApiError } from '../src/http.ts';

const cloudItem = {
  id: 'p1', kind: 'asr', name: '百炼云端识别', type: 'bailian_realtime', base_url: 'wss://ws-….cn-beijing.maas.aliyuncs.com',
  model_id: 'qwen3-asr-flash-realtime', params: {}, enabled: true, is_active: false, is_cloud: true,
  secret_set: true, secret_last4: '3f9a', last_test_at: null, last_test_ok: true, last_test_reason: null,
  version: 2, updated_at: '2026-10-27T00:00:00Z',
};
const active = (confirmed) => ({ asr: { confirmed_cloud_at: confirmed ? '2026-10-27T00:00:00Z' : null, version: 1 },
  llm: { version: 1 }, host: { local_asr_supported: true, egress_policy: 'allowlist', egress_allowlist: [], environment: 'production' } });

test('PUI-02: server rejections become specific Chinese messages', () => {
  const egress = new ApiError(422, 'VALIDATION_ERROR', 'x', { fields: ['base_url'], reason: 'egress_denied' });
  const dup = new ApiError(409, 'PROVIDER_NAME_EXISTS', 'x', { fields: ['name'] });
  const auth = new ApiError(502, 'PROVIDER_TEST_FAILED', 'x', { reason: 'auth_failed' });
  assert.match(providerErrorMessage(egress), /不在部署允许的范围内/);
  assert.equal(providerErrorMessage(dup), '同类中已有同名配置。');
  assert.match(providerErrorMessage(auth), /鉴权失败：请检查 API Key/);
  assert.match(providerErrorMessage(new ApiError(409, 'VERSION_CONFLICT', 'x', { current_version: 3 })), /刷新/);
  assert.match(providerErrorMessage(new ApiError(422, 'VALIDATION_ERROR', 'x', { fields: ['catchup_rate'] })), /补送倍速/);
  assert.match(providerErrorMessage(new ApiError(422, 'VALIDATION_ERROR', 'x', { fields: ['type'], reason: 'host_unsupported' })), /macOS Apple Silicon/);
});

test('create payload never sends an empty key and keeps per-type params', () => {
  const form = { ...emptyForm('asr'), name: ' 云端 ', base_url: 'wss://ws-x.cn-beijing.maas.aliyuncs.com', api_key: '' };
  const body = createPayload('asr', form);
  assert.equal(body.name, '云端');
  assert.ok(!('api_key' in body));
  assert.deepEqual(body.params, { max_record_seconds: 360 });
  // As the type selector does: switching to local clamps the limit to 180.
  const local = createPayload('asr', { ...emptyForm('asr'), type: 'qwen3_mlx', name: '本机', model_id: 'qwen3-asr', base_url: 'x', max_record_seconds: '180' });
  assert.ok(!('base_url' in local));
  assert.deepEqual(local.params, { max_record_seconds: 180 });
});

test('PATCH: omitted key = unchanged, clear = "", new key replaces; unchanged params not resent', () => {
  const base = emptyForm('asr', cloudItem);
  assert.deepEqual(patchPayload(cloudItem, base), { version: 2 });
  assert.deepEqual(patchPayload(cloudItem, { ...base, clear_key: true }), { version: 2, api_key: '' });
  assert.deepEqual(patchPayload(cloudItem, { ...base, api_key: 'sk-test-new' }), { version: 2, api_key: 'sk-test-new' });
  assert.deepEqual(patchPayload(cloudItem, { ...base, name: '新名' }), { version: 2, name: '新名' });
  assert.deepEqual(patchPayload(cloudItem, { ...base, max_record_seconds: '300' }).params, { max_record_seconds: 300 });
});

test('local form checks mirror the server limits', () => {
  const form = { ...emptyForm('asr', cloudItem) };
  assert.equal(formError('asr', form, cloudItem), null);
  assert.match(formError('asr', { ...form, clear_key: true }, cloudItem), /需要 API Key/);
  assert.match(formError('asr', { ...form, max_record_seconds: '361' }, cloudItem), /10–360/);
  assert.match(formError('asr', { ...form, type: 'qwen3_mlx', max_record_seconds: '181' }), /10–180/);
  assert.match(formError('asr', { ...form, name: '' }, cloudItem), /名称/);
});

test('PUI-03/04: cloud confirmation only until confirmed once; row states', () => {
  assert.equal(needsCloudConfirmation(cloudItem, active(false)), true);
  assert.equal(needsCloudConfirmation(cloudItem, active(true)), false);
  assert.equal(needsCloudConfirmation({ ...cloudItem, kind: 'llm', is_cloud: false }, active(false)), false);
  assert.deepEqual(rowStatus({ ...cloudItem, is_active: true }), { label: '当前使用', tone: 'accent' });
  assert.deepEqual(rowStatus({ ...cloudItem, enabled: false }), { label: '已停用', tone: '' });
  assert.deepEqual(rowStatus({ ...cloudItem, last_test_ok: false }), { label: '测试失败', tone: 'warn' });
});
