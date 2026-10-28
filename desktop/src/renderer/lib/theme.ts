export type ThemePref = 'system' | 'light' | 'dark';

const KEY = 'svi.theme';

export function getThemePref(): ThemePref {
  const saved = localStorage.getItem(KEY);
  return saved === 'light' || saved === 'dark' ? saved : 'system';
}

export function applyTheme(pref: ThemePref): void {
  const root = document.documentElement;
  if (pref === 'system') delete root.dataset.theme;
  else root.dataset.theme = pref;
  localStorage.setItem(KEY, pref);
}

export function initTheme(): void {
  const pref = getThemePref();
  if (pref !== 'system') document.documentElement.dataset.theme = pref;
}
