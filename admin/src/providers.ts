/** Provider configuration page logic (V2 FR-11; design §8.1–§8.4). UI-free for tests. */
import { ApiError } from './http.ts';

export type ProviderKind = 'asr' | 'llm';

export interface ProviderItem {
  id: string;
  kind: ProviderKind;
  name: string;
  type: string;
  base_url: string | null;
  model_id: string;
  params: Record<string, number | string | boolean>;
  enabled: boolean;
  is_active: boolean;
  is_cloud: boolean;
  secret_set: boolean;
  secret_last4: string | null;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_reason: string | null;
  version: number;
  updated_at: string;
}

export interface ActiveKind {
  provider: ProviderItem | null;
  source: 'db' | 'yaml' | 'none';
  source_reason: string | null;
  running_provider_id: string | null;
  version: number;
  confirmed_cloud_at: string | null;
  max_record_seconds?: number | null;
  max_concurrent_sessions?: number | null;
}

export interface ActiveView {
  asr: ActiveKind;
  llm: ActiveKind;
  host: { local_asr_supported: boolean; egress_policy: string; egress_allowlist: string[]; environment: string };
}

export interface AuditItem {
  id: string;
  at: string;
  actor_user_id: string;
  action: string;
  kind: ProviderKind;
  provider_id: string | null;
  changed_fields: string[];
  result: 'ok' | 'rejected' | 'failed';
  reason: string | null;
}

export const TYPES: Record<ProviderKind, Array<{ value: string; label: string; short: string }>> = {
  asr: [
    { value: 'bailian_realtime', label: '百炼云端识别（流式）', short: '百炼云端 · 流式' },
    { value: 'qwen3_mlx', label: '本机识别（Qwen3-ASR · 仅 macOS Apple Silicon）', short: '本机 Qwen3-ASR · 仅 Mac' },
  ],
  llm: [
    { value: 'openai_compatible', label: 'OpenAI 兼容接口', short: 'OpenAI 兼容接口' },
    { value: 'ollama', label: 'Ollama（本机/内网）', short: 'Ollama · 本机/内网' },
  ],
};
export const DEV_TYPES: Record<ProviderKind, { value: string; label: string; short: string }> = {
  asr: { value: 'mock', label: '模拟识别（仅开发环境）', short: '模拟识别 · 开发' },
  llm: { value: 'mock', label: '模拟整理（仅开发环境）', short: '模拟整理 · 开发' },
};

export const NEEDS_URL = new Set(['bailian_realtime', 'openai_compatible', 'ollama']);
export const KEYED = new Set(['bailian_realtime', 'openai_compatible']);
export const REQUIRES_KEY = new Set(['bailian_realtime']);
export const LOCAL_TYPES = new Set(['qwen3_mlx', 'mock']);

export function typeShort(kind: ProviderKind, type: string): string {
  return [...TYPES[kind], DEV_TYPES[kind]].find((t) => t.value === type)?.short ?? type;
}

export function recordLimit(type: string): number {
  return type === 'bailian_realtime' ? 360 : 180;
}

const FIELD_LABELS: Record<string, string> = {
  name: '名称', type: '类型', base_url: '地址', model_id: '模型名称', api_key: 'API Key', params: '参数',
  max_record_seconds: '单次录音上限', max_concurrent_sessions: '最大并发', max_inflight: '最大并发',
  catchup_rate: '补送倍速', segment_target_s: '分段目标', max_reconnects: '重连上限', version: '版本',
  confirm_cloud: '隐私确认', provider_id: '配置', kind: '类别',
};

export const TEST_REASONS: Record<string, string> = {
  auth_failed: '鉴权失败：请检查 API Key',
  rate_limited: '云端限流或额度不足，请稍后再试',
  unavailable: '无法连接到服务',
  timeout: '连接超时',
  bad_request: '服务拒绝了请求（参数或协议不匹配）',
};

const SOURCE_REASONS: Record<string, string> = {
  secret_unavailable: '后台保存的密钥无法解密（部署主密钥缺失或不匹配）',
  secret_keys_invalid: '部署主密钥格式无效',
  build_failed: '后台配置无法启动',
  host_unsupported: '当前主机不支持后台选择的本机识别',
  egress_denied: '后台配置的地址不在出口白名单内',
  provider_table_unavailable: '无法读取后台配置',
  yaml_unavailable: '部署 YAML 也没有可用配置',
};

export function sourceReason(reason: string | null): string {
  return reason ? SOURCE_REASONS[reason] ?? '后台配置不可用' : '';
}

export function testReason(reason: string | null): string {
  return reason ? TEST_REASONS[reason] ?? '连接失败' : '';
}

/** Chinese, de-identified message for a provider API error (design §8.4). */
export function providerErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : '操作失败';
  const reason = typeof error.details.reason === 'string' ? error.details.reason : null;
  const fields = Array.isArray(error.details.fields) ? (error.details.fields as string[]) : [];
  switch (error.code) {
    case 'VALIDATION_ERROR':
      if (reason === 'egress_denied') return '该地址不在部署允许的范围内，未保存。如需接入新服务，请联系部署人员修改服务器配置。';
      if (reason === 'host_unsupported') return '当前主机不支持本机识别（仅 macOS Apple Silicon）。';
      if (reason === 'cloud_confirmation_required') return '启用云端识别前需要确认隐私说明。';
      if (fields.includes('api_key')) return '该类型需要 API Key。';
      return fields.length ? `参数不合法：${fields.map((f) => FIELD_LABELS[f] ?? f).join('、')}。` : '参数不合法。';
    case 'PROVIDER_NAME_EXISTS': return '同类中已有同名配置。';
    case 'VERSION_CONFLICT': return '配置已被其他管理员修改，你的修改没有保存。请刷新后重新编辑。';
    case 'PROVIDER_ACTIVE': return '当前使用中的配置不能删除或停用。';
    case 'PROVIDER_DISABLED': return '已停用的配置不能设为当前。';
    case 'PROVIDER_TEST_FAILED': return `连通测试失败：${testReason(reason)}。配置未保存，原配置不变。`;
    case 'NOT_FOUND': return '配置不存在，可能已被删除。请刷新。';
    case 'SERVICE_UNAVAILABLE':
      return reason === 'secret_unavailable' ? '服务端未配置或无法使用密钥加密主密钥，不能保存/读取 API Key。' : error.message;
    default: return error.message || '操作失败';
  }
}

export interface ProviderForm {
  name: string;
  type: string;
  base_url: string;
  model_id: string;
  api_key: string;
  clear_key: boolean;
  max_record_seconds: string;
  max_concurrent: string;
}

export function emptyForm(kind: ProviderKind, item?: ProviderItem): ProviderForm {
  const type = item?.type ?? TYPES[kind][0].value;
  const params = item?.params ?? {};
  const concurrency = kind === 'asr' ? params.max_concurrent_sessions : params.max_inflight;
  return {
    name: item?.name ?? '', type, base_url: item?.base_url ?? '',
    model_id: item?.model_id ?? (kind === 'asr' && type === 'bailian_realtime' ? 'qwen3-asr-flash-realtime' : ''),
    api_key: '', clear_key: false,
    max_record_seconds: String(params.max_record_seconds ?? recordLimit(type)),
    max_concurrent: concurrency === undefined ? '' : String(concurrency),
  };
}

function integer(value: string): number | null {
  if (!/^\d{1,6}$/.test(value.trim())) return null;
  return Number(value.trim());
}

/** Local checks that save a round trip; the server stays authoritative. */
export function formError(kind: ProviderKind, form: ProviderForm, item?: ProviderItem): string | null {
  const name = form.name.trim();
  if (!name || [...name].length > 64) return '名称须为 1–64 个字符。';
  if (NEEDS_URL.has(form.type) && !form.base_url.trim()) return '请填写地址。';
  if (!form.model_id.trim()) return '请填写模型名称。';
  const keyAvailable = form.api_key !== '' || (item?.secret_set && !form.clear_key);
  if (REQUIRES_KEY.has(form.type) && !keyAvailable) return '该类型需要 API Key。';
  if (kind === 'asr') {
    const seconds = integer(form.max_record_seconds);
    if (seconds === null || seconds < 10 || seconds > recordLimit(form.type)) {
      return `单次录音上限须为 10–${recordLimit(form.type)} 秒。`;
    }
  }
  if (form.max_concurrent.trim() !== '' && (integer(form.max_concurrent) ?? 0) < 1) return '最大并发须为正整数。';
  return null;
}

function params(kind: ProviderKind, form: ProviderForm): Record<string, number> {
  const out: Record<string, number> = {};
  const concurrency = integer(form.max_concurrent);
  if (kind === 'asr') {
    out.max_record_seconds = integer(form.max_record_seconds) ?? recordLimit(form.type);
    if (form.type === 'bailian_realtime' && concurrency !== null) out.max_concurrent_sessions = concurrency;
  } else if (concurrency !== null) {
    out.max_inflight = concurrency;
  }
  return out;
}

export function createPayload(kind: ProviderKind, form: ProviderForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    kind, name: form.name.trim(), type: form.type, model_id: form.model_id.trim(), params: params(kind, form),
  };
  if (NEEDS_URL.has(form.type)) body.base_url = form.base_url.trim();
  if (KEYED.has(form.type) && form.api_key) body.api_key = form.api_key;
  return body;
}

/** PATCH body: only changed fields; ``api_key`` absent = unchanged, "" = clear. */
export function patchPayload(item: ProviderItem, form: ProviderForm): Record<string, unknown> {
  const body: Record<string, unknown> = { version: item.version };
  const next = createPayload(item.kind, form);
  if (next.name !== item.name) body.name = next.name;
  if (next.model_id !== item.model_id) body.model_id = next.model_id;
  if (NEEDS_URL.has(form.type) && next.base_url !== item.base_url) body.base_url = next.base_url;
  const current = item.params ?? {};
  const defaults: Record<string, number> = { max_record_seconds: recordLimit(item.type), max_concurrent_sessions: 8, max_inflight: 4 };
  const nextParams = next.params as Record<string, number>;
  if (Object.keys(nextParams).some((key) => nextParams[key] !== (current[key] ?? defaults[key]))) {
    body.params = { ...current, ...nextParams };
  }
  if (form.api_key) body.api_key = form.api_key;
  else if (form.clear_key) body.api_key = '';
  return body;
}

export type RowStatus = { label: string; tone: 'accent' | 'warn' | '' };
export function rowStatus(item: ProviderItem): RowStatus {
  if (item.is_active) return { label: '当前使用', tone: 'accent' };
  if (!item.enabled) return { label: '已停用', tone: '' };
  if (item.last_test_ok === false) return { label: '测试失败', tone: 'warn' };
  return { label: '可用', tone: '' };
}

export const ACTION_LABELS: Record<string, string> = {
  create: '新增', update: '修改', delete: '删除', test: '测试连接', activate: '设为当前', confirm_cloud: '确认云端识别',
};

export function needsCloudConfirmation(item: ProviderItem, active: ActiveView | null): boolean {
  return item.kind === 'asr' && item.is_cloud && !active?.asr.confirmed_cloud_at;
}
