"""One-pass frozen alias normalization. This does not audit model semantics."""
from __future__ import annotations

from collections import Counter
import re

from .dictionary_prompt import norm
from .refine_evidence import NEGATION, NUMBER, PATH, QUOTED, URL


def _pattern(value):
    escaped = re.escape(value)
    if value.isascii():
        escaped = r'(?<![A-Za-z0-9_])' + escaped + r'(?![A-Za-z0-9_])'
    return re.compile(escaped, re.I)


def _protected(text):
    return [(m.start(), m.end()) for p in (QUOTED, PATH, URL, NUMBER, NEGATION) for m in p.finditer(text)]


def _overlap(start, end, spans):
    return any(start < right and end > left for left, right in spans)


def apply_aliases(raw, candidate, terms):
    """Only aliases actually spoken in raw may normalize the final candidate.

    Never change raw or rescan replacements. Term ID/ambiguity comes from the
    frozen full-snapshot selection, not a new query after the model returns.
    """
    valid = [t for t in terms if isinstance(t, dict) and isinstance(t.get('term'), str)
             and t.get('id') and t['term']]
    spellings = [(t['id'], v, _pattern(v)) for t in valid
        for v in [t['term'], *t.get('aliases', [])] if isinstance(v, str) and v]
    raw_protected = _protected(raw)
    blocked = _protected(candidate)
    # An already correct term is never shortened by somebody else's alias.
    blocked.extend((m.start(), m.end()) for t in valid for m in _pattern(t['term']).finditer(candidate))
    edits = []
    for t in valid:
        for alias in t.get('aliases', []):
            if not isinstance(alias, str) or not alias or norm(alias) == norm(t['term']):
                continue
            if norm(alias) in t.get('ambiguous_alias_norms', []):
                continue
            owners = {owner for owner, _, p in spellings if p.fullmatch(alias)}
            if owners != {t['id']}:
                continue
            # Protected tokens may not be added, removed or changed by an alias.
            if any(Counter(p.findall(alias)) != Counter(p.findall(t['term'])) for p in (NUMBER, PATH, URL, NEGATION)):
                continue
            p = _pattern(alias)
            if not any(not _overlap(m.start(), m.end(), raw_protected) for m in p.finditer(raw)):
                continue
            for m in p.finditer(candidate):
                if not _overlap(m.start(), m.end(), blocked):
                    edits.append((m.start(), m.end(), t['term']))
    # Longest match wins an overlap. Stable sorting makes the outcome independent
    # of dictionary insertion order; a conflicting equal span is left unchanged.
    grouped = {}
    for start, end, value in edits:
        grouped.setdefault((start, end), set()).add(value)
    selected = []
    for (start, end), values in sorted(grouped.items(), key=lambda pair: (-(pair[0][1]-pair[0][0]), pair[0][0])):
        if len(values) == 1 and not _overlap(start, end, [(a,b) for a,b,_ in selected]):
            selected.append((start, end, next(iter(values))))
    out, pos = [], 0
    for start, end, value in sorted(selected):
        out.extend((candidate[pos:start], value))
        pos = end
    out.append(candidate[pos:])
    return ''.join(out)
