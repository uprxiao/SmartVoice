export type Role = 'user' | 'admin';
export type AccountStatus = 'active' | 'disabled';

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  status: AccountStatus;
  locale: 'zh-CN' | 'en';
  created_at: string;
  last_login_at: string | null;
}

export interface Settings {
  history_retention_days: number | null;
  learning_enabled: boolean;
  locale: 'zh-CN' | 'en';
  version: number;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: 'Bearer';
  expires_in: number;
  refresh_expires_at: string;
  user: User;
}

export interface AuthSnapshot {
  user: User;
  settings: Settings;
  /** True when the OS keychain is unavailable and tokens are memory-only. */
  memory_only?: boolean;
}

export type SessionState =
  | 'created'
  | 'streaming'
  | 'finalizing'
  | 'refining'
  | 'ready'
  | 'empty'
  | 'failed'
  | 'cancelled';

export interface SessionResult {
  result_id: string;
  attempt_no: number;
  attempt_kind: 'initial' | 'asr' | 'refine';
  raw_text: string;
  refined_text: string;
  review_required: boolean;
  review_reason: string | null;
  text_hash: string;
  dictionary_version: number;
  stop_to_result_ms: number;
  created_at: string;
}

export interface Session {
  session_id: string;
  mode: 'dictation';
  state: SessionState;
  current_attempt_no: number;
  /** Frozen effective learning permission stored when this session was created. */
  learning_enabled: boolean;
  current_result_id: string | null;
  audio_complete: boolean;
  received_samples: number;
  audio_ms: number;
  /** V2: recording limit fixed at creation; absent/null on older servers. */
  max_record_seconds?: number | null;
  created_at: string;
  first_terminal_at: string | null;
  content_expires_at: string | null;
  content_expired: boolean;
  error_code: string | null;
  result: SessionResult | null;
  retry_kind: 'asr' | 'refine' | 'none';
  retry_expires_at: string | null;
  retry_unavailable_reason: string | null;
  insertion_status: 'reserved' | 'inserted' | 'failed' | 'unverified' | null;
}

export interface Insertion {
  replayed: boolean;
  insertion_id: string;
  session_id: string;
  result_id: string;
  status: 'reserved' | 'inserted' | 'failed' | 'unverified';
  observable: boolean;
  error_code: string | null;
  claimed_at: string;
  completed_at: string | null;
}

export interface TermContext {
  alias_norm: string;
  before: string;
  after: string;
  left: string;
  right: string;
}

export interface Term {
  id: string;
  term: string;
  norm: string;
  aliases: string[];
  contexts: TermContext[];
  scope: 'default';
  source: 'manual' | 'auto';
  version: number;
  dictionary_version: number;
  created_at: string;
  updated_at: string;
  ambiguous_aliases: string[];
}

export interface LearningSuggestion extends LearningEvent {
  before: string;
  after: string;
}

export interface LearningEvent {
  event_id: string;
  correction_id: string;
  status: 'pending' | 'learned' | 'suggested' | 'ignored' | 'failed';
  reason: string | null;
  classification: 'term' | 'suggest' | 'ignore' | null;
  ambiguous: boolean | null;
  term: Term | null;
  term_id: string | null;
  after_version: number | null;
  undone: boolean;
  undo_available: boolean;
  created_at: string;
}

export interface StatsTotals {
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

export interface DailyStat {
  date: string;
  sessions: number;
  chars: number;
  successful_audio_ms: number;
  heat_level: number;
}

export interface Stats {
  timezone: string;
  date_from: string;
  date_to: string;
  totals: StatsTotals;
  active_days_in_range: number;
  current_streak_days: number;
  longest_streak_days: number;
  dictionary_terms: number;
  cumulative_learning_events: number;
  daily: DailyStat[];
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface DictionaryPage extends Page<Term> {
  dictionary_version: number;
}

export interface SessionStartResult {
  session_id: string;
  attempt_no: number;
  /** This session's PCM sample limit (server-granted); absent = 180 s fallback. */
  max_samples?: number;
  insertion_available: boolean;
  target_app: string | null;
  target_error: string | null;
}

export interface InsertionOutcome {
  status: 'inserted' | 'failed' | 'unverified' | 'unavailable';
  observable: boolean;
  error_code: string | null;
  message: string;
  insertion_id: string | null;
  review_required: boolean;
}

export interface AppInfo {
  version: string;
  electron: string;
  platform: NodeJS.Platform;
  accessibilityTrusted: boolean;
  accessibilitySupported: boolean;
  apiBaseUrl: string;
  shortcut: string;
}

export interface SessionProgress {
  acknowledged_samples: number;
  session_id: string | null;
  attempt_no: number | null;
  revision: number;
  auto_insert_allowed: boolean;
  /** Main-local capture handshake; not a server session/WS state. */
  capture_ready?: boolean;
  /** Local target metadata only; no target handle, original text or selection crosses IPC. */
  target?: Pick<SessionStartResult, 'insertion_available' | 'target_app' | 'target_error'>;
  state: SessionState;
  audio_ms: number;
  /** Recording limit of the current session in ms (absent = 180 s default). */
  max_audio_ms?: number;
  stable_text: string;
  provisional_text: string;
  error_code: string | null;
  result: SessionResult | null;
}
