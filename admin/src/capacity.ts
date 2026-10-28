import { ApiError } from './http.ts';
import type { CapacityView } from './types.ts';

export function capacityInput(value: string, ceiling: number): number {
  const trimmed = value.trim();
  const parsed = Number(trimmed);
  if (!/^\d+$/.test(trimmed) || !Number.isSafeInteger(parsed) || parsed < 1 || parsed > Math.min(ceiling, 2_147_483_647)) {
    throw new Error(`请输入 1–${Math.min(ceiling, 2_147_483_647)} 之间的整数`);
  }
  return parsed;
}

export function validationSummary(view: CapacityView | null): {
  label: string; description: string; exceeds: boolean;
} {
  if (!view) return { label: '未知', description: '尚未取得容量验证状态。', exceeds: false };
  const { status, report } = view.validation;
  if (status === 'unverified' || !report || !Number.isSafeInteger(report.target_sessions) || report.target_sessions < 1) {
    return { label: '未验证', description: '尚无有效容量报告，不能据此承诺并发性能。', exceeds: false };
  }
  const exceeds = view.config.max_active_sessions > report.target_sessions;
  return {
    label: status === 'stale' ? '报告已过期' : '已验证',
    description: status === 'stale'
      ? `原报告验证 ${report.target_sessions} 路；当前资源或配置指纹已变化，需要重新验证。`
      : `报告已验证 ${report.target_sessions} 路。验证时间：${new Date(report.tested_at).toLocaleString('zh-CN')}。`,
    exceeds,
  };
}

export function percentileLabel(runtime: CapacityView['runtime'], percentile: 'p50' | 'p95'): string {
  if (!runtime) return '未知';
  const value = runtime[`stop_to_result_${percentile}_ms`];
  if (value !== null) return `${value} ms`;
  if (runtime.failed_count > 0) return '含失败，无法给出有效分位';
  return runtime.sample_count === 0 ? '无样本' : '未知';
}

type CapacityClient = {
  capacity(): Promise<CapacityView>;
  updateCapacity(version: number, value: number): Promise<CapacityView>;
};
export type SaveCapacityOutcome =
  | { kind: 'saved' | 'confirmed'; view: CapacityView; message: string }
  | { kind: 'review'; view: CapacityView | null; message: string };

/** Reconcile uncertain delivery with GET. Never re-send PATCH or silently rebase CAS. */
export async function saveCapacity(
  client: CapacityClient, version: number, value: number
): Promise<SaveCapacityOutcome> {
  try {
    const view = await client.updateCapacity(version, value);
    if (view.config.max_active_sessions !== value) {
      return { kind: 'review', view, message: '返回的生效值与提交值不同，请读取当前配置并重新比较。' };
    }
    return { kind: 'saved', view, message: `已保存：${value} 路，版本 ${view.config.version}。` };
  } catch (error) {
    const conflict = error instanceof ApiError && error.status === 409;
    const uncertain = !(error instanceof ApiError) || error.status === 0 || error.status >= 500
      || error.status === 408 || error.code === 'INVALID_RESPONSE';
    if (!conflict && !uncertain) throw error;
    let view: CapacityView;
    try {
      view = await client.capacity();
    } catch (readError) {
      if (readError instanceof ApiError && [401, 403].includes(readError.status)) throw readError;
      return {
        kind: 'review', view: null,
        message: '未能确认保存结果，读取生效配置也失败。输入已保留；请先读取当前配置，勿直接重复保存。',
      };
    }
    if (!conflict && view.config.max_active_sessions === value && view.config.version >= version) {
      return {
        kind: 'confirmed', view,
        message: `保存响应未确认；重新读取已确认当前生效 ${value} 路，版本 ${view.config.version}。`,
      };
    }
    return {
      kind: 'review', view,
      message: `${conflict ? '容量配置已被修改' : '保存响应未确认'}。当前生效 ${view.config.max_active_sessions} 路（版本 ${view.config.version}）；输入已保留，请读取当前配置并重新比较。`,
    };
  }
}
