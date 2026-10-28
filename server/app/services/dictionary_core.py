from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path


class Conflict(ValueError):
    pass


def normalized(text: str) -> str:
    return unicodedata.normalize('NFKC', text).strip().casefold()


def term_text(text: str) -> str:
    text = unicodedata.normalize('NFC', text).strip()
    if not text or len(text) > 64 or any(unicodedata.category(c).startswith('C') for c in text):
        raise ValueError('词条不能为空、超过 64 字或包含控制字符')
    return text


class Dictionary:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
          INSERT OR IGNORE INTO meta VALUES ('version',0);
          CREATE TABLE IF NOT EXISTS terms (
            id TEXT PRIMARY KEY, term TEXT NOT NULL, norm TEXT NOT NULL, aliases TEXT NOT NULL,
            scope TEXT NOT NULL, source TEXT NOT NULL, version INTEGER NOT NULL,
            updated_at REAL NOT NULL, UNIQUE(norm,scope));
          CREATE TABLE IF NOT EXISTS learning_events (
            id TEXT PRIMARY KEY, correction_id TEXT UNIQUE NOT NULL, term_id TEXT NOT NULL,
            before_json TEXT, after_version INTEGER NOT NULL, undone INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL);
        ''')
        # Additive migration: older test databases remain readable.
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(terms)')}
        if 'contexts' not in columns:
            self.db.execute("ALTER TABLE terms ADD COLUMN contexts TEXT NOT NULL DEFAULT '[]'")
        self.db.execute('PRAGMA user_version=2')
        self.db.commit()

    @property
    def version(self) -> int:
        return self.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]

    def bump(self):
        self.db.execute("UPDATE meta SET value=value+1 WHERE key='version'")

    @staticmethod
    def row(row):
        if row is None:
            return None
        value = dict(row)
        value.pop('norm', None)
        value['aliases'] = json.loads(value['aliases'])
        value['contexts'] = json.loads(value.get('contexts', '[]'))
        return value

    def get(self, term_id: str):
        return self.row(self.db.execute('SELECT * FROM terms WHERE id=?', (term_id,)).fetchone())

    def list(self, search='', scope='default'):
        # Literal substring search, no wildcard expansion from user input.
        rows = self.db.execute('SELECT * FROM terms WHERE scope=? ORDER BY updated_at DESC LIMIT 2000', (scope,))
        return [value for row in rows if (value := self.row(row)) and
                (not search or normalized(search) in normalized(value['term'] + ' ' + ' '.join(value['aliases'])))]

    def put(self, term: str, aliases: list[str], scope='default', source='manual', term_id=None, version=None):
        with self.db:
            return self._put(term, aliases, scope, source, term_id, version)

    def _put(self, term: str, aliases: list[str], scope='default', source='manual', term_id=None, version=None):
        term = term_text(term)
        aliases = list(dict.fromkeys(term_text(x) for x in aliases if x.strip()))
        aliases = [x for x in aliases if normalized(x) != normalized(term)]
        if len(aliases) > 20 or len(scope) > 100:
            raise ValueError('词条线索或范围过多')
        old = self.get(term_id) if term_id else self.row(self.db.execute(
            'SELECT * FROM terms WHERE norm=? AND scope=?', (normalized(term), scope)).fetchone())
        if term_id and not old:
            raise Conflict('词条已删除，请刷新')
        if version is not None and (not old or old['version'] != version):
            raise Conflict('词条已被修改，请刷新后重试')
        if old and not term_id:
            aliases = list(dict.fromkeys(old['aliases'] + aliases))[:20]
        final_source = 'manual' if source == 'manual' or (old and old['source'] == 'manual') else 'auto'
        new_id = old['id'] if old else str(uuid.uuid4())
        new_version = old['version'] + 1 if old else 1
        try:
            self.db.execute('''INSERT INTO terms (id,term,norm,aliases,scope,source,version,updated_at) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET term=excluded.term,norm=excluded.norm,aliases=excluded.aliases,
                    source=excluded.source,version=excluded.version,updated_at=excluded.updated_at''',
                    (new_id, term, normalized(term), json.dumps(aliases, ensure_ascii=False), scope,
                     final_source, new_version, time.time()))
            if old:
                # Editing/removing an alias must remove its former learning context too.
                contexts = [c for c in old['contexts'] if c['alias'] in aliases] if normalized(old['term']) == normalized(term) else []
                self.db.execute('UPDATE terms SET contexts=? WHERE id=?', (json.dumps(contexts, ensure_ascii=False), new_id))
            self.bump()
        except sqlite3.IntegrityError as e:
            raise Conflict('相同范围已存在此词条') from e
        return self.get(new_id)

    def delete(self, term_id, version):
        with self.db:
            changed = self.db.execute('DELETE FROM terms WHERE id=? AND version=?', (term_id, version)).rowcount
            if not changed:
                raise Conflict('词条已更改或删除，请刷新')
            self.bump()

    def learn(self, correction_id, before, after, scope='default', *, left='', right=''):
        event = self.db.execute('SELECT * FROM learning_events WHERE correction_id=?', (correction_id,)).fetchone()
        if event:
            return {'event_id': event['id'], 'term': self.get(event['term_id']), 'undone': bool(event['undone'])}
        old = self.row(self.db.execute('SELECT * FROM terms WHERE norm=? AND scope=?', (normalized(after), scope)).fetchone())
        event_id = str(uuid.uuid4())
        with self.db:
            term = self._put(after, [before], scope, 'auto')
            if left or right:
                context = {'alias': before, 'left': left[-80:], 'right': right[:80]}
                contexts = [c for c in term['contexts'] if c != context]
                contexts = [*contexts, context][-3:]
                self.db.execute('UPDATE terms SET contexts=? WHERE id=?', (json.dumps(contexts, ensure_ascii=False), term['id']))
                term = self.get(term['id'])
            self.db.execute('INSERT INTO learning_events VALUES (?,?,?,?,?,0,?)',
                (event_id, correction_id, term['id'], json.dumps(old, ensure_ascii=False) if old else None,
                 term['version'], time.time()))
        return {'event_id': event_id, 'term': term}

    def undo(self, event_id):
        event = self.db.execute('SELECT * FROM learning_events WHERE id=?', (event_id,)).fetchone()
        if not event:
            raise ValueError('学习记录不存在')
        if event['undone']:
            return {'undone': True}
        current = self.get(event['term_id'])
        if not current or current['version'] != event['after_version']:
            raise Conflict('词条后来已更改；请在词库中编辑，避免覆盖新的修改')
        old = json.loads(event['before_json']) if event['before_json'] else None
        with self.db:
            if old:
                self.db.execute('UPDATE terms SET term=?,aliases=?,contexts=?,source=?,version=version+1,updated_at=? WHERE id=?',
                    (old['term'], json.dumps(old['aliases'], ensure_ascii=False), json.dumps(old.get('contexts', []), ensure_ascii=False), old['source'], time.time(), current['id']))
            else:
                self.db.execute('DELETE FROM terms WHERE id=?', (current['id'],))
            self.db.execute('UPDATE learning_events SET undone=1 WHERE id=?', (event_id,))
            self.bump()
        return {'undone': True}

    def relevant(self, text: str, limit=50):
        def matches(term):
            # English words require boundaries, so an alias "ai" does not match "email".
            for word in [term['term'], *term['aliases']]:
                pattern = re.escape(word)
                if word.isascii():
                    # Chinese and Latin words are often adjacent in mixed dictation.
                    # Block an embedded ASCII token (cloudy), not Chinese (cloud来).
                    pattern = r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])'
                if re.search(pattern, text, re.I):
                    return True
            return False
        return [t for t in self.list() if matches(t)][:limit]

    def close(self):
        self.db.close()
