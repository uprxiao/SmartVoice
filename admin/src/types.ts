export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: 'user' | 'admin';
  status: 'active' | 'disabled';
  locale: string;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: 'Bearer';
  expires_in: number;
  refresh_expires_at: string;
  user: User;
}

export interface AdminUserItem {
  user: User;
  totals: {
    sessions: number;
    received_audio_ms: number;
    result_sessions: number;
    last_session_at: string | null;
  };
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface StatTotals {
  sessions: number;
  received_audio_ms: number;
  result_sessions: number;
  failed_sessions: number;
  empty_sessions: number;
  cancelled_sessions: number;
  chars: number;
  successful_audio_ms: number;
  average_chars_per_minute: number;
  fallback_sessions: number;
  inserted_sessions: number;
  processing_success_rate: number;
  insertion_success_rate: number;
  fallback_rate: number;
  learning_events: number;
}

export interface Overview {
  timezone: string;
  date_from: string;
  date_to: string;
  registered_users: number;
  active_users: number;
  disabled_users: number;
  distinct_active_users: number;
  totals: StatTotals;
  daily: Array<{
    date: string;
    daily_active_users: number;
    sessions: number;
    received_audio_ms: number;
    result_sessions: number;
    failed_sessions: number;
    empty_sessions: number;
    cancelled_sessions: number;
  }>;
}

export interface UsageItem {
  date: string;
  user_id: string;
  sessions: number;
  received_audio_ms: number;
  result_sessions: number;
  chars: number;
  successful_audio_ms: number;
  fallback_sessions: number;
  inserted_sessions: number;
  learning_events: number;
}

export interface ModelUsage {
  date: string;
  provider: string;
  model_id: string;
  purpose: string;
  calls: number;
  successes: number;
  failures: number;
  unknown_calls: number;
  elapsed_ms: number;
  processed_audio_ms: number;
  prompt_tokens_known: number;
  completion_tokens_known: number;
  prompt_tokens_unknown_calls: number;
  completion_tokens_unknown_calls: number;
}

export interface CapacityView {
  config: {
    version: number;
    max_active_sessions: number;
    updated_at: string;
    updated_by: string | null;
  };
  deployment: {
    admission_ceiling: number;
    effective_ceiling: number;
    active_pcm_budget_bytes: number;
    retry_pcm_budget_bytes: number;
    asr_execution_slots: number;
    llm_max_inflight: number;
  };
  runtime: null | {
    sampled_at: string;
    active_sessions: number;
    available_sessions: number;
    draining: boolean;
    asr_running: number;
    asr_partial_waiting: number;
    asr_finish_waiting: number;
    llm_running: number;
    llm_foreground_waiting: number;
    llm_classify_waiting: number;
    active_pcm_bytes: number;
    reserved_pcm_bytes: number;
    retry_pcm_bytes: number;
    window_seconds: number;
    sample_count: number;
    rejected_count: number;
    failed_count: number;
    fallback_count: number;
    stop_to_result_p50_ms: number | null;
    stop_to_result_p95_ms: number | null;
  };
  validation: { status: 'unverified' | 'valid' | 'stale'; report: CapacityReport | null };
}

/** Read-only deployment report; mirrors API contract §7.1. Never authored by the admin UI. */
export interface CapacityReport {
  report_id: string;
  tested_at: string;
  target_sessions: number;
  environment_fingerprint: string;
  app_commit: string;
  config_sha256: string;
  fixture_manifest_sha256: string;
  hardware_model_manifest_sha256: string;
  evidence_sha256: string;
  normal_completed: number;
  normal_total: number;
  rejected: number;
  failed: number;
  fallback: number;
  stop_to_result_p50_ms: number;
  stop_to_result_p95_ms: number;
  desktop_stop_to_readback_p50_ms: number;
  desktop_stop_to_readback_p95_ms: number;
  soak_passed: boolean;
}
