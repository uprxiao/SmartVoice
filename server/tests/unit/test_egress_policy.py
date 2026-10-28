"""Outbound allowlist rules (V2 design §6.3)."""
import pytest

from app.core.egress import EgressDenied, EgressPolicy, redact_host

ALLOW = ['*.maas.aliyuncs.com', 'api.deepseek.com']


def policy(mode='allowlist', answers=('8.8.8.8',)):
    async def resolver(host, port):
        return list(answers)
    return EgressPolicy(mode, ALLOW, resolver=resolver)


@pytest.mark.parametrize('url', [
    'wss://ws-abc123.cn-beijing.maas.aliyuncs.com',
    'wss://dashscope.maas.aliyuncs.com/api-ws/v1/realtime',
])
def test_wildcard_matches_subdomains(url):
    assert policy().check_url(url, kind='asr').port == 443


@pytest.mark.parametrize('url', [
    'wss://maas.aliyuncs.com',              # wildcard does not match the apex
    'wss://evilmaas.aliyuncs.com',          # dot boundary
    'wss://ws-a.maas.aliyuncs.com.evil.io',
    'ws://ws-a.maas.aliyuncs.com',          # public ASR must be wss
    'https://ws-a.maas.aliyuncs.com',       # wrong scheme for ASR
    'wss://user:pw@ws-a.maas.aliyuncs.com',  # userinfo
    'wss://8.8.8.8',                         # IP literal not listed
    'wss://ws-a.maas.aliyuncs.com:8443',     # non-443 public port
])
def test_rejected_asr_urls(url):
    with pytest.raises(EgressDenied):
        policy().check_url(url, kind='asr')


def test_deny_public_rejects_allowlisted_host():
    with pytest.raises(EgressDenied):
        policy('deny-public').check_url('wss://ws-a.maas.aliyuncs.com', kind='asr')


def test_local_addresses_follow_v1_rules_in_both_modes():
    for mode in ('deny-public', 'allowlist'):
        assert policy(mode).check_url('ws://127.0.0.1:9000', kind='asr').local
        assert policy(mode).check_url('http://10.1.2.3:11434', kind='llm').local


def test_llm_https_exact_host():
    assert not policy().check_url('https://api.deepseek.com', kind='llm').local
    with pytest.raises(EgressDenied):
        policy().check_url('http://api.deepseek.com', kind='llm')
    with pytest.raises(EgressDenied):
        policy().check_url('https://x.api.deepseek.com', kind='llm')


@pytest.mark.parametrize('answer', ['10.0.0.5', '127.0.0.1', '169.254.1.1', '100.64.0.1', '192.168.1.1', '::1'])
async def test_public_entry_resolving_to_non_public_is_denied(answer):
    p = policy(answers=('8.8.8.8', answer))
    target = p.check_url('wss://ws-a.maas.aliyuncs.com', kind='asr')
    with pytest.raises(EgressDenied):
        await p.resolve(target)


async def test_resolve_returns_validated_public_ip():
    p = policy(answers=('8.8.8.8',))
    assert await p.resolve(p.check_url('wss://ws-a.maas.aliyuncs.com', kind='asr')) == '8.8.8.8'


def test_redact_workspace():
    assert redact_host('wss://ws-abc123.cn-beijing.maas.aliyuncs.com') == 'wss://ws-….cn-beijing.maas.aliyuncs.com'
