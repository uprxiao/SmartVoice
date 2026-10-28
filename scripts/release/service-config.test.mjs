import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { root } from './common.mjs';
import { serviceBuildConfig, serviceBuildEnvironment } from './service-config.mjs';

const config = JSON.parse(readFileSync(join(root, 'release.config.json'), 'utf8'));
test('all installer targets receive the committed HTTPS service and CA pin', () => {
  const service = serviceBuildConfig();
  const env = serviceBuildEnvironment(service, {});
  assert.equal(env.VITE_API_BASE_URL, config.service.apiBaseUrl);
  assert.equal(env.VITE_SERVICE_CA_SHA256, config.service.caSha256);
  assert.match(env.VITE_API_BASE_URL, /^https:\/\//);
});

for (const address of ['http://192.168.14.151', 'https://127.0.0.1', 'https://centos91', 'https://user:password@fixture.invalid', 'https://fixture.invalid?test=1']) {
  test(`installer refuses an unusable or ambiguous distribution address: ${address}`, () => {
    assert.throws(() => serviceBuildConfig({ ...config, service: { ...config.service, apiBaseUrl: address } }));
  });
}

test('installer refuses missing service config, mismatched CA and conflicting environment overrides', () => {
  assert.throws(() => serviceBuildConfig({ channel: 'preview' }), /apiBaseUrl/);
  assert.throws(() => serviceBuildConfig({ ...config, service: { ...config.service, caSha256: '0'.repeat(64) } }), /mismatch/);
  assert.throws(() => serviceBuildEnvironment(serviceBuildConfig(), { VITE_API_BASE_URL: 'https://centos91' }), /must match/);
});
