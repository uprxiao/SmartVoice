import { defineEvent } from './ipc/events';
import type { LearningSuggestion, SessionProgress } from './types';

export const sessionProgressEvent = defineEvent<SessionProgress>('session.progress');
export const sessionResultEvent = defineEvent<SessionProgress>('session.result');
export const sessionErrorEvent = defineEvent<string>('session.error');
export const authChangedEvent = defineEvent<null>('auth.changed');
export const dictationLevelEvent = defineEvent<number>('dictation.level');
export const hotkeyToggleEvent = defineEvent<null>('hotkey.toggle');
export const hudConfirmEvent = defineEvent<null>('hud.confirm');
export const hudCancelEvent = defineEvent<null>('hud.cancel');
export const learningSuggestedEvent = defineEvent<LearningSuggestion>('learning.suggested');
export const learningLearnedEvent = defineEvent<LearningSuggestion>('learning.learned');

/** HUD-only notices; never carries the result text (copy happens in Main). */
export interface HudNotice {
  kind: 'permission' | 'insert-failed';
  message: string;
  permission: boolean;
}
export const HUD_NOTICE_MS = 8_000;
export const hudNoticeEvent = defineEvent<HudNotice | null>('hud.notice');
export const hudCopyEvent = defineEvent<null>('hud.copy');
export const hudOpenAccessibilityEvent = defineEvent<null>('hud.open-accessibility');
export const hudDismissEvent = defineEvent<null>('hud.dismiss');
