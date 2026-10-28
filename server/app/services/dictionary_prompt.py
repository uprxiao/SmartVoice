"""Frozen dictionary selection; full snapshot remains attempt-owned memory only."""
from __future__ import annotations
import json
import logging
import re
import unicodedata


def norm(value):
    return unicodedata.normalize('NFKC', value).casefold()


def asr_hints(snapshot):
    selected, used = [], 0
    for entry in sorted(snapshot, key=lambda e: (-e['updated_timestamp'], e['norm'], e['id'])):
        term = entry['term']
        cost = len(term) + bool(selected)
        if len(selected) < 100 and used + cost <= 2000:
            selected.append(term); used += cost
    if len(selected) != len(snapshot):
        logging.getLogger(__name__).info('ASR dictionary hints omitted: %d', len(snapshot) - len(selected))
    return selected


def _matches(text, candidate):
    pattern = re.escape(candidate)
    if candidate.isascii():
        pattern = r'(?<![A-Za-z0-9_])' + pattern + r'(?![A-Za-z0-9_])'
    return bool(re.search(pattern, text, re.I))


# A rejection-only caseless key. Python regex treats dotted/dotless I as
# equivalent to ASCII i; ordinary casefold alone does not. Full casefold can
# admit extra candidates (e.g. ss/ß), so the original regex remains authoritative.
_IGNORECASE_I = str.maketrans('İı', 'ii')


def _prefilter_key(value):
    if 'İ' in value or 'ı' in value:
        value = value.translate(_IGNORECASE_I)
    return value.casefold()


def refine_hints(snapshot, raw):
    folded_raw = _prefilter_key(raw)

    def matches(value):
        if _prefilter_key(value) not in folded_raw:
            return False
        return _matches(raw, value)

    owners = {}
    for entry in snapshot:
        for value in [entry['term'], *entry['aliases']]:
            owners.setdefault(norm(value), set()).add(entry['id'])
    matched = []
    for entry in snapshot:
        hits = [len(value) for value in [entry['term'], *entry['aliases']] if matches(value)]
        if hits:
            matched.append((max(hits), entry))
    matched.sort(key=lambda pair: (-pair[0], -pair[1]['updated_timestamp'], pair[1]['norm'], pair[1]['id']))
    selected = []
    for _, entry in matched:
        item = {key: entry[key] for key in ('id', 'term', 'norm', 'aliases')}
        item['contexts'] = entry.get('contexts', [])[:3]
        item['ambiguous_alias_norms'] = sorted({norm(alias) for alias in entry['aliases'] if len(owners[norm(alias)]) != 1})
        proposed = [*selected, item]
        # Both HTTP adapters serialize request.input with default JSON spacing.
        # Include those separators in the model-visible terms budget as well as
        # alias/context/ID fields. Never truncate a term or a bound context.
        if len(proposed) <= 50 and len(json.dumps(proposed, ensure_ascii=False)) <= 4000:
            selected = proposed
    if len(selected) != len(matched):
        logging.getLogger(__name__).info('Refine dictionary hints omitted: %d', len(matched) - len(selected))
    return selected
