import { useEffect, useRef, useState, type ReactNode } from 'react';
import { observer } from 'mobx-react-lite';
import {
  Accessibility,
  Activity,
  Check,
  ExternalLink,
  Globe2,
  HelpCircle,
  Info,
  Keyboard,
  Loader2,
  LogOut,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  UserRound,
  X,
} from 'lucide-react';
import type { AppInfo } from '@shared/types';
import { API_BASE_URL } from '@shared/config';
import { stores } from '../stores';
import { RetentionControl } from '../components/RetentionControl';
import { errorMessage } from '../lib/error-message';
import { rpc } from '../lib/ipc';
import { watchAppInfo } from '../lib/app-info-refresh';
import { applyTheme, getThemePref, type ThemePref } from '../lib/theme';
import { tr, type Locale } from '../lib/i18n';
import { acceleratorFromKeyEvent, friendlyShortcut } from '../lib/shortcut';

type SectionKey = 'account' | 'settings' | 'appearance' | 'about' | 'help';

const SECTIONS: Array<{ key: SectionKey; labelKey: string; icon: typeof UserRound }> = [
  { key: 'account', labelKey: 'settings.nav.account', icon: UserRound },
  { key: 'settings', labelKey: 'settings.nav.general', icon: Settings2 },
  { key: 'appearance', labelKey: 'settings.nav.personalization', icon: SlidersHorizontal },
  { key: 'about', labelKey: 'settings.nav.about', icon: Info },
  { key: 'help', labelKey: 'settings.nav.help', icon: HelpCircle },
];

const THEMES: Array<{ value: ThemePref; labelKey: string }> = [
  { value: 'system', labelKey: 'settings.theme.system' },
  { value: 'light', labelKey: 'settings.theme.light' },
  { value: 'dark', labelKey: 'settings.theme.dark' },
];

function healthReason(locale: Locale, reason: string): string {
  const code = reason.trim();
  if (code === 'STARTING') return tr(locale, 'settings.health.reasonStarting');
  if (/fetch failed|ECONNREFUSED|ECONNRESET|ENOTFOUND|network/i.test(code)) {
    return tr(locale, 'settings.health.reasonUnreachable');
  }
  if (code === 'ASR_UNAVAILABLE') return tr(locale, 'settings.health.reasonAsr');
  if (code === 'LLM_UNAVAILABLE') return tr(locale, 'settings.health.reasonLlm');
  if (code === 'CAPACITY_CONFIG_INVALID' || code === 'CAPACITY_EXCEEDS_DEPLOYMENT') return tr(locale, 'settings.health.reasonCapacity');
  if (code === 'SERVICE_UNAVAILABLE') return tr(locale, 'settings.health.reasonService');
  return reason;
}

function ShortcutRecorder({
  value,
  locale,
  platform,
  onApply,
}: {
  value: string;
  locale: Locale;
  platform: NodeJS.Platform;
  onApply: (next: string) => Promise<void>;
}) {
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!recording) return;
    const handler = (event: KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === 'Escape') {
        setRecording(false);
        return;
      }
      const accelerator = acceleratorFromKeyEvent(event, platform);
      if (!accelerator) return;
      setRecording(false);
      setBusy(true);
      // onApply shows the save error; consume that rejection here so a normal
      // reserved-key rejection does not also become an unhandled Promise.
      void onApply(accelerator).catch(() => undefined).finally(() => setBusy(false));
    };
    window.addEventListener('keydown', handler, true);
    return () => window.removeEventListener('keydown', handler, true);
  }, [recording, onApply, platform]);

  return (
    <button
      type="button"
      className={`settings-control flex w-[240px] items-center justify-between ${
        recording ? 'ring-2 ring-[var(--accent)]' : ''
      }`}
      onClick={() => {
        setRecording(true);
      }}
      disabled={busy}
    >
      <span className={recording ? 'text-[var(--text-3)]' : ''}>
        {busy
          ? tr(locale, 'settings.shortcut.saving')
          : recording
            ? tr(locale, 'settings.shortcut.recording')
            : friendlyShortcut(value, platform)}
      </span>
      {busy ? <Loader2 size={13} className="animate-spin" /> : <Keyboard size={14} className="text-[var(--text-3)]" />}
    </button>
  );
}

function Group({
  title,
  icon,
  children,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="mt-9 first:mt-0">
      <div className="flex items-center gap-2.5 border-b border-[var(--border-thin)] pb-3 text-[17px] font-medium text-[var(--text-3)]">
        <span className="text-[var(--text-3)]">{icon}</span>
        {title}
      </div>
      <div className="mt-1">{children}</div>
    </section>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-8 py-5 first:pt-0 last:pb-0">
      <div className="min-w-0 flex-1">
        <div className="text-[15px] font-semibold leading-6">{label}</div>
        {hint && (
          <div className="mt-1 max-w-[640px] text-[14px] leading-6 text-[var(--text-3)]">{hint}</div>
        )}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

type ServiceHealth = Awaited<ReturnType<typeof rpc.app.health>>;

// The same service panel is rendered by Settings and the focused SSR regression.
export function SettingsServiceHealth({ locale, apiBaseUrl, health, checking, onRecheck }: {
  locale: Locale;
  apiBaseUrl: string | null;
  health: ServiceHealth | null;
  checking: boolean;
  onRecheck: () => void | Promise<void>;
}) {
  const healthStatus = checking ? 'checking' : (health?.status ?? 'error');
  const loading = health?.status === 'loading';
  const healthTone = healthStatus === 'ready' ? 'accent'
    : healthStatus === 'degraded' ? 'warn'
      : healthStatus === 'loading' || healthStatus === 'checking' ? '' : 'danger';
  const dateLocale = locale === 'en' ? 'en-US' : 'zh-CN';
  return (
    <Group title={tr(locale, 'settings.health.group')} icon={<Activity size={15} />}>
      <Row label={tr(locale, 'settings.health.label')} hint={tr(locale, 'settings.health.hint')}>
        <div className="flex items-center gap-2">
          <span role="status" className={`pill ${healthTone}`}>
            {tr(locale, `settings.health.${healthStatus}`)}
          </span>
          <button className="btn-secondary" onClick={() => void onRecheck()} disabled={checking}>
            {tr(locale, 'settings.health.recheck')}
          </button>
        </div>
      </Row>
      <div className="mt-1 space-y-1.5 border-t border-[var(--border-thin)] pt-3 text-[13px] leading-5 text-[var(--text-3)]">
        <div className="flex items-center justify-between gap-4">
          <span>{tr(locale, 'settings.about.server')}</span>
          <span className="font-mono text-[12px] text-[var(--text)]">{apiBaseUrl ?? '—'}</span>
        </div>
        {health?.asrModel && (
          <div className="flex items-center justify-between gap-4">
            <span>ASR · LLM</span>
            <span className="text-right text-[var(--text)]">
              {tr(locale, 'settings.health.models', {
                asr: health.asrModel,
                llm: loading ? health.llmModel ?? '—' : health.llmAvailable
                  ? `${health.llmModel ?? '—'} (${tr(locale, 'settings.health.llmAvailable')})`
                  : tr(locale, 'settings.health.llmUnavailable'),
              })}
            </span>
          </div>
        )}
        {health?.reason && (
          <div className={loading ? 'text-[var(--text-3)]' : 'text-[var(--danger)]'}>
            {healthReason(locale, health.reason)}
          </div>
        )}
        {health && (
          <div className="text-[12px]">
            {tr(locale, 'settings.health.lastChecked', {
              time: new Date(health.checkedAt).toLocaleTimeString(dateLocale),
            })}
          </div>
        )}
      </div>
    </Group>
  );
}

export const SettingsView = observer(function SettingsView({ onClose }: { onClose: () => void }) {
  const { auth, session, ui } = stores;
  const [section, setSection] = useState<SectionKey>('settings');
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [platformChecking, setPlatformChecking] = useState(true);
  const shortcut = ui.localShortcut ?? '';
  const [savingShortcut, setSavingShortcut] = useState(false);
  const [theme, setTheme] = useState<ThemePref>(() => getThemePref());
  const [health, setHealth] = useState<ServiceHealth | null>(null);
  const [checking, setChecking] = useState(true);

  const refreshInfo = useRef<() => void>(() => undefined);

  useEffect(() => {
    const watcher = watchAppInfo(() => ui.loadAppInfo(), value => { setInfo(value); setPlatformChecking(false); });
    refreshInfo.current = watcher.refresh;
    return () => { refreshInfo.current = () => undefined; watcher.stop(); };
  }, [ui]);

  const checkHealth = async () => {
    setChecking(true);
    try {
      // Keep the checking state visible long enough to avoid a misleading
      // green flash when the local server answers instantly.
      const [next] = await Promise.all([
        rpc.app.health(),
        new Promise((resolve) => setTimeout(resolve, 300)),
      ]);
      setHealth(next);
    } catch {
      setHealth(null);
    } finally {
      setChecking(false);
    }
  };

  useEffect(() => {
    void checkHealth();
  }, []);

  const settings = auth.settings;
  const locale = (settings?.locale ?? 'zh-CN') as Locale;
  const isWindows = info?.platform === 'win32';
  const observationUnsupported = info?.correctionObservationSupported === false;
  const autoStart = ui.appInfo?.autoStart;
  const openSystem = async (action: () => Promise<unknown>) => {
    try { await action(); } catch { ui.toast(tr(locale, 'windows.settingsFailed'), 'error'); }
  };
  const dateLocale = locale === 'en' ? 'en-US' : 'zh-CN';
  const apply = async (patch: {
    learning_enabled?: boolean;
    locale?: 'zh-CN' | 'en';
  }) => {
    if (!settings) return;
    setBusy(true);
    try {
      await auth.updateSettings({ version: settings.version, ...patch });
      ui.toast(tr(locale, 'settings.saved'));
    } catch (error) {
      ui.toast(errorMessage(error) || tr(locale, 'settings.saveFailed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const applyShortcut = async (next: string) => {
    try {
      const applied = await ui.updateShortcut(next);
      ui.toast(tr(locale, 'settings.shortcut.updated', { value: friendlyShortcut(applied, ui.appInfo?.platform) }));
    } catch (error) {
      ui.toast(errorMessage(error) || tr(locale, 'settings.shortcut.saveFailed'), 'error');
      throw error;
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-8 backdrop-blur-[2px]"
      onMouseDown={onClose}
    >
      <div
        className="flex h-[min(980px,92vh)] max-h-full w-[min(1290px,92vw)] max-w-full overflow-hidden rounded-[24px] bg-[var(--dialog-bg)] shadow-[0_30px_90px_rgba(0,0,0,0.45)]"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <aside className="flex w-[260px] shrink-0 flex-col p-8">
          <h1 className="text-[22px] font-semibold leading-7 tracking-[-0.3px]">
            {tr(locale, 'settings.title')}
          </h1>
          <nav className="mt-8 flex flex-col gap-1">
            {SECTIONS.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.key}
                  className={`settings-nav-item gap-3 ${section === item.key ? 'active' : ''}`}
                  onClick={() => setSection(item.key)}
                >
                  <Icon size={17} strokeWidth={1.7} />
                  {tr(locale, item.labelKey)}
                </button>
              );
            })}
          </nav>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-start justify-between px-10 pt-8">
            <h2 className="text-[22px] font-semibold leading-7 tracking-[-0.3px]">
              {tr(locale, SECTIONS.find((item) => item.key === section)?.labelKey ?? 'settings.title')}
            </h2>
            <button
              className="btn-ghost -mr-2 -mt-1 h-9 w-9 px-0"
              onClick={onClose}
              aria-label={tr(locale, 'settings.close')}
            >
              <X size={18} />
            </button>
          </div>
          <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-10 pb-10 pt-6">
            {section === 'account' && (
              <>
                <Group title={tr(locale, 'settings.account.group')} icon={<UserRound size={15} />}>
                  <div className="flex items-center gap-3">
                    <div className="flex h-11 w-11 items-center justify-center rounded-full bg-[var(--surface-strong)] text-[15px] font-semibold">
                      {(auth.user?.display_name || auth.user?.email || 'U').slice(0, 1).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-[15px] font-semibold">
                        {auth.user?.display_name || auth.user?.email.split('@')[0]}
                      </div>
                      <div className="text-[14px] text-[var(--text-3)]">{auth.user?.email}</div>
                    </div>
                    <span className="pill">
                      {auth.user?.role === 'admin'
                        ? tr(locale, 'settings.account.admin')
                        : tr(locale, 'settings.account.user')}
                    </span>
                  </div>
                  <div className="mt-4 grid grid-cols-2 gap-2.5 text-[14px]">
                    <div className="rounded-xl bg-[var(--surface-strong)] px-3.5 py-2.5">
                      <div className="text-[12px] text-[var(--text-3)]">
                        {tr(locale, 'settings.account.registered')}
                      </div>
                      <div className="mt-0.5">
                        {auth.user ? new Date(auth.user.created_at).toLocaleDateString(dateLocale) : '—'}
                      </div>
                    </div>
                    <div className="rounded-xl bg-[var(--surface-strong)] px-3.5 py-2.5">
                      <div className="text-[12px] text-[var(--text-3)]">
                        {tr(locale, 'settings.account.lastLogin')}
                      </div>
                      <div className="mt-0.5">
                        {auth.user?.last_login_at
                          ? new Date(auth.user.last_login_at).toLocaleString(dateLocale)
                          : '—'}
                      </div>
                    </div>
                  </div>
                </Group>
                <Group title={tr(locale, 'settings.session.group')} icon={<LogOut size={15} />}>
                  <div className="flex items-center justify-between gap-4">
                    <div className="text-[14px] leading-6 text-[var(--text-3)]">
                      {tr(locale, 'settings.session.hint')}
                    </div>
                    <button
                      className="btn-secondary"
                      disabled={busy}
                      onClick={async () => {
                        await session.reset().catch(() => undefined);
                        try { await auth.logout(); ui.toast('已退出登录'); }
                        catch { /* AuthView displays the stored logout/persistence error. */ }
                      }}
                    >
                      {busy ? <Loader2 size={13} className="animate-spin" /> : <LogOut size={13} />}{' '}
                      {tr(locale, 'settings.session.signOut')}
                    </button>
                  </div>
                </Group>
              </>
            )}

            {section === 'settings' && (
              <>
                <Group title={tr(locale, 'settings.shortcut.group')} icon={<Keyboard size={15} />}>
                  <Row
                    label={tr(locale, 'settings.shortcut.label')}
                    hint={tr(locale, 'settings.shortcut.hint')}
                  >
                    <ShortcutRecorder value={shortcut} locale={locale} platform={ui.appInfo?.platform ?? 'darwin'} onApply={applyShortcut} />
                  </Row>
                  {isWindows && <Row label={tr(locale, 'windows.startup.label')} hint={tr(locale, 'windows.startup.hint')}>
                    <div className="flex flex-col items-end gap-2">
                      <button role="switch" aria-label={tr(locale, 'windows.startup.label')}
                        aria-checked={autoStart?.registered ?? false}
                        disabled={!autoStart?.configurable || autoStart.error || ui.autoStartBusy}
                        className={`relative h-[22px] w-[38px] rounded-full transition ${autoStart?.registered ? 'bg-[var(--accent)]' : 'bg-[var(--track)]'}`}
                        onClick={async () => {
                          try { await ui.updateAutoStart(!autoStart?.registered); ui.toast(tr(locale, 'settings.saved')); }
                          catch (error) { ui.toast(tr(locale, `windows.${errorMessage(error)}`), 'error'); }
                        }}>
                        <span className={`absolute top-[2px] h-[18px] w-[18px] rounded-full bg-[var(--thumb)] shadow transition-all ${autoStart?.registered ? 'left-[18px]' : 'left-[2px]'}`} />
                      </button>
                      {ui.autoStartBusy && <span role="status">{tr(locale, 'settings.shortcut.saving')}</span>}
                      <span className="text-[12px] text-[var(--text-3)]">{tr(locale, !autoStart?.configurable ? 'windows.startup.development'
                        : autoStart.error ? 'windows.checkError' : autoStart.systemDisabled ? 'windows.startup.blocked'
                        : autoStart.enabled ? 'windows.startup.enabled' : 'windows.startup.disabled')}</span>
                      {autoStart?.systemDisabled && <button className="btn-ghost" onClick={() => void openSystem(rpc.app.openStartupSettings)}>{tr(locale, 'windows.startup.open')}</button>}
                      {autoStart?.error && <button className="btn-ghost" onClick={() => refreshInfo.current()}>{tr(locale, 'windows.recheck')}</button>}
                    </div>
                  </Row>}

                </Group>

                <Group title={tr(locale, 'settings.language.group')} icon={<Globe2 size={15} />}>
                  <Row
                    label={tr(locale, 'settings.language.label')}
                    hint={tr(locale, 'settings.language.hint')}
                  >
                    <select
                      className="settings-control w-[240px]"
                      value={settings?.locale ?? 'zh-CN'}
                      disabled={!settings || busy}
                      onChange={(event) =>
                        void apply({ locale: event.target.value as 'zh-CN' | 'en' })
                      }
                    >
                      <option value="zh-CN">简体中文（中国大陆）</option>
                      <option value="en">English</option>
                    </select>
                  </Row>
                </Group>

                <Group title={tr(locale, 'settings.learning.group')} icon={<ShieldCheck size={15} />}>
                  <Row
                    label={tr(locale, 'settings.learning.label')}
                    hint={tr(locale, observationUnsupported ? 'windows.learning.hint' : 'settings.learning.hint')}
                  >
                    {observationUnsupported || !info ? <span className="pill">{tr(locale, !info ? 'windows.checking' : 'windows.learning.unavailable')}</span> : <button
                      role="switch"
                      aria-checked={settings?.learning_enabled ?? false}
                      disabled={!settings || busy}
                      onClick={() => void apply({ learning_enabled: !settings?.learning_enabled })}
                      className={`relative h-[22px] w-[38px] rounded-full transition ${
                        settings?.learning_enabled ? 'bg-[var(--accent)]' : 'bg-[var(--track)]'
                      }`}
                    >
                      <span
                        className={`absolute top-[2px] h-[18px] w-[18px] rounded-full bg-[var(--thumb)] shadow transition-all ${
                          settings?.learning_enabled ? 'left-[18px]' : 'left-[2px]'
                        }`}
                      />
                    </button>}
                  </Row>
                  <div className="mt-2 border-t border-[var(--border-thin)] pt-2">
                    <Row
                      label={tr(locale, 'settings.retention.label')}
                      hint={tr(locale, 'settings.retention.hint')}
                    >
                      <RetentionControl
                        locale={locale}
                        className="settings-control w-[240px]"
                        disabled={busy}
                      />
                    </Row>
                  </div>
                </Group>

                <Group title={tr(locale, 'settings.permissions.group')} icon={<Accessibility size={15} />}>
                  <Row label={tr(locale, 'windows.microphone.label')} hint={tr(locale, isWindows ? 'windows.microphone.hint' : 'windows.microphone.macHint')}>
                    <div className="flex max-w-[290px] flex-wrap justify-end gap-2">
                      <span className={`pill ${info?.microphone.status === 'granted' ? 'accent' : 'warn'}`} role="status">{tr(locale, platformChecking ? 'windows.checking' : info?.microphone.error ? 'windows.checkError'
                        : `windows.microphone.${info?.microphone.status ?? 'unknown'}`)}</span>
                      <button className="btn-ghost" disabled={platformChecking} onClick={() => { setPlatformChecking(true); refreshInfo.current(); }}>{tr(locale, 'windows.recheck')}</button>
                      <button className="btn-secondary" onClick={() => void openSystem(rpc.app.openMicrophoneSettings)}>{tr(locale, 'windows.microphone.open')}</button>
                    </div>
                  </Row>
                  {isWindows && <Row label={tr(locale, 'windows.input.label')} hint={tr(locale, 'windows.input.hint')}><span className="pill">{tr(locale, info?.nativeInputSupported ? 'windows.input.ordinary' : 'windows.input.unavailable')}</span></Row>}
                  {info?.platform === 'darwin' && <div className="flex items-start gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--surface-strong)]">
                      <Accessibility size={16} className="text-[var(--text-3)]" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[14px] font-medium">
                        {tr(locale, 'settings.permissions.label')}
                        <span className={`pill ${info?.accessibilityTrusted ? 'accent' : 'warn'}`}>
                          {info?.accessibilityTrusted
                            ? tr(locale, 'settings.permissions.trusted')
                            : tr(locale, 'settings.permissions.missing')}
                        </span>
                      </div>
                      <div className="mt-1 max-w-[640px] text-[14px] leading-6 text-[var(--text-3)]">
                        {tr(locale, 'settings.permissions.hint')}
                      </div>
                      <div className="mt-2 flex gap-2">
                        <button
                          className="btn-secondary"
                          onClick={async () => {
                            try {
                              await rpc.app.requestAccessibility();
                              refreshInfo.current();
                            } catch (error) {
                              ui.toast(
                                error instanceof Error
                                  ? error.message
                                  : tr(locale, 'settings.permission.requestFailed'),
                                'error'
                              );
                            }
                          }}
                        >
                          {tr(locale, 'settings.permissions.request')}
                        </button>
                        <button
                          className="btn-ghost"
                          onClick={() => void rpc.app.openAccessibilitySettings()}
                        >
                          {tr(locale, 'settings.permissions.open')} <ExternalLink size={12} />
                        </button>
                      </div>
                    </div>
                  </div>}
                </Group>

                <SettingsServiceHealth locale={locale} apiBaseUrl={info?.apiBaseUrl ?? null}
                  health={health} checking={checking} onRecheck={checkHealth} />
              </>
            )}

            {section === 'appearance' && (
              <Group title={tr(locale, 'settings.theme.group')} icon={<SlidersHorizontal size={15} />}>
                <Row label={tr(locale, 'settings.theme.label')} hint={tr(locale, 'settings.theme.hint')}>
                  <div className="segmented">
                    {THEMES.map((item) => (
                      <button
                        key={item.value}
                        className={`segmented-item ${theme === item.value ? 'active' : ''}`}
                        onClick={() => {
                          setTheme(item.value);
                          applyTheme(item.value);
                        }}
                      >
                        {tr(locale, item.labelKey)}
                      </button>
                    ))}
                  </div>
                </Row>
              </Group>
            )}

            {section === 'about' && (
              <Group title={tr(locale, 'settings.about.group')} icon={<Info size={15} />}>
                <div className="grid grid-cols-2 gap-2.5 text-[14px]">
                  <div className="flex items-center gap-2 text-[var(--text-3)]">
                    <ShieldCheck size={13} /> {tr(locale, 'settings.about.clientVersion')}
                    <span className="ml-auto text-[var(--text)]">{info?.version ?? '—'}</span>
                  </div>
                  <div className="flex items-center gap-2 text-[var(--text-3)]">
                    Electron
                    <span className="ml-auto text-[var(--text)]">{info?.electron ?? '—'}</span>
                  </div>
                  <div className="col-span-2 flex items-center gap-2 rounded-lg bg-[var(--surface-strong)] px-3 py-2 text-[var(--text-3)]">
                    {tr(locale, 'settings.about.server')}
                    <span className="ml-auto font-mono text-[12px] text-[var(--text)]">
                      {info?.apiBaseUrl ?? API_BASE_URL}
                    </span>
                  </div>
                  {info?.platform === 'darwin' && <div className="col-span-2 flex items-center gap-2 rounded-lg bg-[var(--surface-strong)] px-3 py-2 text-[var(--text-3)]">
                    {tr(locale, 'settings.permissions.label')}
                    <span className="ml-auto flex items-center gap-1 text-[var(--text)]">
                      {info?.accessibilityTrusted ? <Check size={13} /> : null}
                      {info?.accessibilityTrusted
                        ? tr(locale, 'settings.permissions.trusted')
                        : tr(locale, 'settings.permissions.missing')}
                    </span>
                  </div>}
                </div>
              </Group>
            )}

            {section === 'help' && (
              <Group title={tr(locale, 'settings.help.group')} icon={<HelpCircle size={15} />}>
                <Row
                  label={tr(locale, info?.platform === 'darwin' ? 'settings.help.permissions.title' : 'windows.input.label')}
                  hint={tr(locale, info?.platform === 'darwin' ? 'settings.help.permissions.hint' : 'windows.input.hint')}
                >
                  <button
                    className="btn-secondary"
                    onClick={() => void openSystem(info?.platform === 'darwin' ? rpc.app.openAccessibilitySettings : rpc.app.openMicrophoneSettings)}
                  >
                    {tr(locale, info?.platform === 'darwin' ? 'settings.permissions.open' : 'windows.microphone.open')} <ExternalLink size={12} />
                  </button>
                </Row>
                <Row
                  label={tr(locale, 'settings.help.shortcut.title')}
                  hint={tr(locale, 'settings.help.shortcut.hint')}
                >
                  <span className="pill">{friendlyShortcut(shortcut, ui.appInfo?.platform)}</span>
                </Row>
                {isWindows && <Row label={tr(locale, 'windows.install.label')} hint={tr(locale, 'windows.install.hint')}><span /></Row>}
                <Row
                  label={tr(locale, 'settings.help.recovery.title')}
                  hint={tr(locale, 'settings.help.recovery.hint')}
                >
                  <span />
                </Row>
              </Group>
            )}
          </div>
        </div>
      </div>
    </div>
  );
});
