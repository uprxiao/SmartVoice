import type { AppInfo } from '@shared/types';

export function prepareCaptureAccess(_isCurrent: () => boolean): void {}
export function microphoneInfo(): AppInfo['microphone'] {
  return { status: 'not-required', error: false };
}
export async function openMicrophoneSettings(): Promise<void> {
  throw new Error('模拟版无需麦克风');
}
