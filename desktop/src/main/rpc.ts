import { app, dialog, shell, systemPreferences } from 'electron';
import { open } from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { createRPCController, createRPCRouter } from '@shared/ipc/rpc';
import type { ImportResult } from './core/api-client';
import type { ApiClient } from './core/api-client';
import type { AuthService } from './core/auth-service';
import type { NativeVoiceInput } from './core/native-input';
import type { SessionService } from './core/session-service';
import type { ShortcutService } from './core/shortcut-service';
import type { HudNotice } from '@shared/events';
import { autoStartInfo, microphoneInfo, openMicrophoneSettings, openStartupSettings, updateAutoStart } from '../../native/system-settings';

export interface RpcDeps {
  api: ApiClient;
  auth: AuthService;
  sessions: SessionService;
  native: NativeVoiceInput;
  shortcuts: ShortcutService;
  /** Non-activating HUD notices (main window may be hidden behind the target app). */
  notifyHud?: (notice: HudNotice | null) => void;
}

export const ACCESSIBILITY_MISSING_HINT = '未开启辅助功能，结果不会自动输入';
const PERMISSION_FAILED_HINT = '未开启辅助功能，结果未输入';

export function createRpcRouter(deps: RpcDeps) {
  let activeStart: { operationId: string; userId: string; authVersion: number } | null = null;
  const sameAccount = (userId: string, version: number): boolean => deps.auth.current?.user.id === userId && deps.auth.contextVersion === version;
  const currentSession = (sessionId: string): void => {
    deps.auth.ensure();
    if (typeof sessionId !== 'string' || !sessionId || deps.sessions.currentSessionId() !== sessionId) throw new Error('语音会话已变化，请重新操作');
  };
  return createRPCRouter({
    app: createRPCController({
      info: async () => {
        let trusted = false;
        let supported = process.platform === 'darwin';
        try {
          const permission = await deps.native.call('permission', { prompt: false });
          trusted = Boolean(permission.trusted);
          supported = Boolean(permission.supported);
        } catch {
          trusted = false;
        }
        return {
          version: app.getVersion(),
          electron: process.versions.electron,
          platform: process.platform,
          accessibilityTrusted: process.platform === 'darwin' && trusted,
          accessibilitySupported: process.platform === 'darwin' && supported,
          nativeInputSupported: supported,
          correctionObservationSupported: deps.native.supportsObservation ?? process.platform === 'darwin',
          microphone: microphoneInfo(),
          autoStart: autoStartInfo(),
          apiBaseUrl: deps.api.baseUrl,
          shortcut: deps.shortcuts.current,
        };
      },
      health: async () => {
        try {
          const caps = await deps.api.capabilities();
          return {
            status: !caps.ready && caps.unavailable_reason === 'STARTING'
              ? 'loading' : caps.ready ? 'ready' : caps.degraded ? 'degraded' : 'error',
            ready: caps.ready,
            degraded: caps.degraded,
            reason: caps.unavailable_reason,
            asrModel: caps.asr.model_id,
            llmAvailable: caps.llm.available,
            llmModel: caps.llm.model_id,
            checkedAt: new Date().toISOString(),
          };
        } catch (error) {
          const status = (error as { status?: number }).status;
          return {
            status: status ? 'error' : 'unreachable',
            ready: false,
            degraded: false,
            reason: error instanceof Error ? error.message : String(error),
            asrModel: null,
            llmAvailable: null,
            llmModel: null,
            checkedAt: new Date().toISOString(),
          };
        }
      },
      updateShortcut: (accelerator: string) => deps.shortcuts.update(accelerator),
      requestAccessibility: () => process.platform === 'darwin' ? deps.native.call('permission', { prompt: true }) : { supported: false, trusted: false },
      openAccessibilitySettings: async () => {
        if (process.platform !== 'darwin') throw new Error('SYSTEM_SETTINGS_UNAVAILABLE');
        await shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
      },
      openMicrophoneSettings,
      openStartupSettings,
      updateAutoStart,
      openExternal: async (url: string) => {
        if (!/^https?:\/\//i.test(url)) throw new Error('只允许打开 http(s) 链接');
        await shell.openExternal(url);
      },
    }),
    auth: createRPCController({
      register: (email: string, password: string) => deps.auth.register(email, password),
      login: (email: string, password: string) => deps.auth.login(email, password),
      logout: () => deps.auth.logout(),
      me: () => deps.auth.current,
      snapshot: () => deps.auth.current,
    }),
    settings: createRPCController({
      get: () => deps.auth.ensure().settings,
      update: (patch: { version: number; history_retention_days?: number | null; learning_enabled?: boolean; locale?: 'zh-CN' | 'en' }) =>
        deps.auth.updateSettings(patch),
    }),
    session: createRPCController({
      start: async (learningEnabled: boolean, operationId: string) => {
        if (typeof operationId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(operationId)) throw new Error('无效的录音操作');
        if (deps.sessions.isBusy()) throw new Error('已有进行中的语音会话');
        const binding = { operationId, userId: deps.auth.ensure().user.id, authVersion: deps.auth.contextVersion };
        activeStart = binding;
        try {
          // An OS permission prompt has no audio to upload. Do not create a
          // server session (and spend its audio-idle budget) while it is open.
          if (process.platform === 'darwin' || process.platform === 'win32') {
            const microphoneStatus = () => {
              try { return systemPreferences.getMediaAccessStatus('microphone'); }
              catch { throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE'); }
            };
            let status = microphoneStatus();
            if (status === 'not-determined' && process.platform === 'darwin') {
              let granted: boolean;
              try { granted = await systemPreferences.askForMediaAccess('microphone'); }
              catch { throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE'); }
              if (activeStart !== binding || !sameAccount(binding.userId, binding.authVersion)) throw new Error('录音操作已取消');
              if (!granted) throw new Error('MICROPHONE_PERMISSION_DENIED');
              status = microphoneStatus();
            }
            if (status === 'denied' || status === 'restricted') throw new Error('MICROPHONE_PERMISSION_DENIED');
            if (status !== 'granted') throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE');
          }
          if (activeStart !== binding || !sameAccount(binding.userId, binding.authVersion)) throw new Error('录音操作已取消');
          if (deps.sessions.isBusy()) throw new Error('已有进行中的语音会话');
          deps.notifyHud?.(null);
          const started = await deps.sessions.start(learningEnabled);
          // Query only (no system prompt mid-dictation: it would steal focus from the target).
          if (process.platform === 'darwin' && deps.notifyHud) {
            void deps.native.call('permission', { prompt: false }).then(permission => {
              if (permission.supported !== false && permission.trusted === false && deps.sessions.isBusy()
                && sameAccount(binding.userId, binding.authVersion)) {
                deps.notifyHud?.({ kind: 'permission', message: ACCESSIBILITY_MISSING_HINT, permission: true });
              }
            }, () => undefined);
          }
          return started;
        }
        catch (error) { if (activeStart === binding) activeStart = null; throw error; }
      },
      cancelStart: (operationId: string) => {
        const binding = activeStart;
        if (!binding || binding.operationId !== operationId || !sameAccount(binding.userId, binding.authVersion)) return;
        activeStart = null;
        deps.sessions.reset();
      },
      captureReady: (sessionId: string, attemptNo: number) => { currentSession(sessionId); return deps.sessions.captureReady(sessionId, attemptNo); },
      finish: (sessionId: string) => { currentSession(sessionId); return deps.sessions.finish(); },
      cancel: (sessionId: string) => {
        currentSession(sessionId);
        const cancelled = deps.sessions.cancel();
        if (process.platform === 'win32') deps.native.invalidate();
        return cancelled;
      },
      retry: (sessionId: string, kind: 'asr' | 'refine') => { currentSession(sessionId); return deps.sessions.retry(kind); },
      insert: async (sessionId: string, reviewConfirmed: boolean, explicit = true) => {
        currentSession(sessionId);
        const outcome = await deps.sessions.insert(reviewConfirmed, explicit);
        // Automatic insertion runs while the user is in another app: surface a
        // proven zero-write on the HUD. Unverified may have written; no HUD alarm.
        if (!explicit && outcome.status === 'failed') {
          const permission = process.platform === 'darwin' && outcome.error_code === 'PERMISSION_DENIED';
          deps.notifyHud?.({ kind: 'insert-failed', permission,
            ...(process.platform === 'win32' ? { locale: deps.auth.current?.settings?.locale ?? 'zh-CN' } : {}),
            message: permission ? PERMISSION_FAILED_HINT : outcome.message || '未能自动输入，可复制结果' });
        }
        return outcome;
      },
      retarget: (sessionId: string, delayMs?: number) => { currentSession(sessionId); return deps.sessions.retarget(delayMs); },
      copy: (text: string) => deps.sessions.copy(text),
      status: () => deps.sessions.current(),
      reset: (sessionId: string) => { currentSession(sessionId); return deps.sessions.reset(); },
    }),
    history: createRPCController({
      selectForRetry: (sessionId: string, kind: 'asr' | 'refine') => deps.sessions.selectForRetry(sessionId, kind),
      list: (cursor?: string | null, limit?: number) => deps.api.history(cursor, limit),
    }),
    stats: createRPCController({
      me: (dateFrom: string, dateTo: string) => deps.api.stats(dateFrom, dateTo),
    }),
    learning: createRPCController({
      get: (eventId: string) => deps.api.getLearningEvent(eventId),
      confirm: (eventId: string) => deps.api.confirmLearningEvent(eventId, randomUUID()),
      undo: (eventId: string) => deps.api.undoLearningEvent(eventId, randomUUID()),
    }),
    dictionary: createRPCController({
      list: (params: {
        q?: string;
        source?: 'all' | 'manual' | 'auto';
        cursor?: string | null;
        limit?: number;
      }) => deps.api.dictionary(params),
      create: (term: string, aliases: string[]) =>
        deps.api.createTerm(term, aliases, randomUUID()),
      update: (id: string, payload: { version: number; term?: string; aliases?: string[] }) =>
        deps.api.updateTerm(id, payload, randomUUID()),
      remove: (id: string, version: number) =>
        deps.api.deleteTerm(id, version, randomUUID()),
      removeMany: (items: { id: string; version: number }[], requestKey: string) =>
        deps.api.deleteTerms(items, requestKey),
      importCsv: async (): Promise<ImportResult | null> => {
        const userId = deps.auth.ensure().user.id, version = deps.auth.contextVersion;
        const selected = await dialog.showOpenDialog({
          title: '导入词库 CSV',
          properties: ['openFile'],
          filters: [{ name: 'CSV', extensions: ['csv'] }],
        });
        if (!sameAccount(userId, version)) throw new Error('账户已变化，已取消导入');
        if (selected.canceled || !selected.filePaths[0]) return null;
        const path = selected.filePaths[0];
        const file = await open(path, fsConstants.O_RDONLY | fsConstants.O_NONBLOCK);
        let bytes: Buffer;
        try {
          const info = await file.stat();
          if (!info.isFile() || info.size > 1024 * 1024) throw new Error('CSV 文件必须为不超过 1 MiB 的普通文件');
          const buffer = Buffer.alloc(1024 * 1024 + 1);
          let length = 0;
          while (length < buffer.length) {
            if (!sameAccount(userId, version)) throw new Error('账户已变化，已取消导入');
            const part = await file.read(buffer, length, buffer.length - length, null);
            if (part.bytesRead === 0) break;
            length += part.bytesRead;
          }
          if (length > 1024 * 1024) throw new Error('CSV 文件超过 1 MiB');
          bytes = buffer.subarray(0, length);
        } finally { await file.close(); }
        if (!sameAccount(userId, version)) throw new Error('账户已变化，已取消导入');
        return deps.api.importCsv(path.split(/[\\/]/).pop() ?? 'terms.csv', bytes, randomUUID());
      },
    }),
  });
}

export type RpcRouter = ReturnType<typeof createRpcRouter>;
