import { AuthStore } from './auth-store';
import { DictionaryStore } from './dictionary-store';
import { HistoryStore } from './history-store';
import { SessionStore } from './session-store';
import { StatsStore } from './stats-store';
import { UiStore } from './ui-store';

const ui = new UiStore();
const auth = new AuthStore();
const session = new SessionStore(auth, ui);
const history = new HistoryStore();
const dictionary = new DictionaryStore();
const stats = new StatsStore();

export const stores = { ui, auth, session, history, dictionary, stats };
