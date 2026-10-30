// Integration entry only. Keep production classes/functions intact; the owning
// fixture supplies a disposable Electron profile and real Windows processes.
export { NativeVoiceInput } from '../../../src/main/core/native-input';
export { WindowsTray } from '../../../src/main/app/windows-tray';
export { SessionService } from '../../../src/main/core/session-service';
export { InsertionJournal } from '../../../src/main/core/insertion-journal';
export { WS_BASE_URL } from '../../../src/shared/config';
export {
  createMainWindow, createHudWindow, getMainWindow, getHudWindow,
  showHud, hideHud, markAppQuitting,
} from '../../../src/main/app/window';
