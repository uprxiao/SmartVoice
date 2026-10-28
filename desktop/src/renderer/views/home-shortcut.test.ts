import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const viewState = vi.hoisted(() => ({
  ui: { localShortcut: null as string | null },
  session: { phase: 'idle', result: null, insertionAvailable: false, targetError: null, progress: { audio_ms: 0 }, partialText: '', level: 0 },
  stats: { stats: null, error: null },
  history: { items: [], error: null },
  auth: { settings: { locale: 'zh-CN', learning_enabled: false } },
}));
vi.mock('../stores', () => ({ stores: viewState }));
import { HomeView } from './HomeView';
import { tr } from '../lib/i18n';

// The existing node Vitest config has no React plugin; support its classic JSX transform.
vi.stubGlobal('React', React);
afterAll(() => { vi.unstubAllGlobals(); });
beforeEach(() => { viewState.ui.localShortcut = null; viewState.session.phase = 'idle'; viewState.auth.settings.locale = 'zh-CN'; });

describe('Home renders the actual device shortcut in both places', () => {
  it('shows unknown while Main has not supplied a shortcut', () => {
    const html = renderToStaticMarkup(React.createElement(HomeView, { onNavigate: () => undefined }));
    expect(html).toContain(tr('zh-CN', 'home.shortcutOr', { value: '—' }));
    // Shortcut is now rendered as a key cap, rather than an unstyled div.
    expect(html.match(/<kbd\b[^>]*>([^<]*)<\/kbd>/)?.[1]).toBe('—');
    expect(html).not.toContain('⌘⇧Space');
  });

  it('shows the restored F19 instead of the default in both Home hints', () => {
    viewState.ui.localShortcut = 'F19';
    const html = renderToStaticMarkup(React.createElement(HomeView, { onNavigate: () => undefined }));
    expect(html).toContain(tr('zh-CN', 'home.shortcutOr', { value: 'F19' }));
    expect(html.match(/F19/g)).toHaveLength(2);
    expect(html).not.toContain('⌘⇧Space');
  });
});


describe('Home reflects local preparation instead of listening', () => {
  it.each(['zh-CN', 'en'] as const)('keeps a cancellable preparing view in %s', locale => {
    viewState.session.phase = 'starting'; viewState.auth.settings.locale = locale;
    const html = renderToStaticMarkup(React.createElement(HomeView, { onNavigate: () => undefined }));
    expect(html).toContain(tr(locale, 'home.preparing'));
    expect(html).toContain(tr(locale, 'home.preparingHint'));
    expect(html).toContain(tr(locale, 'home.cancelRecording'));
    expect(html).not.toContain(tr(locale, 'home.listening'));
    expect(html).not.toContain(tr(locale, 'home.stopAndRefine'));
    expect(html).not.toContain('recording-pulse');
  });
});
