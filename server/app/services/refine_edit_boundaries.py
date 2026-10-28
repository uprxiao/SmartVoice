"""Archived exact-quote boundary guards, reused by opt-in edits-only only.

Constants and _complete_final are byte-for-byte extracted from the separately
reviewed unit-boundary candidate. No old exact-quote protocol is enabled.
"""
from __future__ import annotations

import re

from .refine_evidence import ENGLISH, NEGATION, NUMBER, PATH, URL

_FINAL_BRIDGE = re.compile(
    r'(?:\s|[,，:：;；。.!！？?]|应该是|准确说|改成|改为|改用|我重说|重新说|是|'
    r'it should be|it is|use)*', re.I)
# Bound each simple number plus known adjacent unit against the original raw,
# independently of where the model stops final. This does not parse compound
# units or prove semantic completeness; the original program guards still apply.
_FINAL_NUMBER = re.compile(r'[+-]?\d+(?:[.:%/-]\d+)*|[零一二两三四五六七八九十百千万亿]+')
_FINAL_UNIT_WORDS = (
    '毫秒', '微秒', '纳秒', '分钟', '小时', '千米', '厘米', '毫米', '千克', '公斤', '毫克',
    '摄氏度', '华氏度', '字节', '秒', '分', '时', '天', '次', '个', '项', '条', '份', '台',
    '米', '克', '吨', '升', '毫升', 'KiB', 'MiB', 'GiB', 'TiB', 'MHz', 'GHz', 'KB', 'MB',
    'GB', 'TB', 'Hz', 'ms', 'min', 'ns', 'us', 'µs', 'μs', 'km', 'cm', 'mm', 'kg', 'mg',
    's', 'h', 'm', 'g', 'B', 'b', '%', '％', '℃', '°C', '°F',
)
# Longest first: matching a prefix of a longer known unit cannot shorten its span.
_FINAL_UNIT = re.compile(r'\s*(?:' + '|'.join(
    re.escape(unit) for unit in sorted(_FINAL_UNIT_WORDS, key=len, reverse=True)
) + r')(?![A-Za-z])')



def _complete_final(raw, trigger_end, final_start, final_end):
    if not _FINAL_BRIDGE.fullmatch(raw[trigger_end:final_start]):
        raise ValueError('FINAL_OMITS_PREFIX_CONSTRAINT')
    for pattern in (PATH, URL, NUMBER, ENGLISH, NEGATION):
        for match in pattern.finditer(raw):
            if final_start < match.end() and final_end > match.start():
                if not final_start <= match.start() < match.end() <= final_end:
                    raise ValueError('FINAL_SPLITS_PROTECTED_LITERAL')
    for number in _FINAL_NUMBER.finditer(raw):
        unit = _FINAL_UNIT.match(raw, number.end())
        if unit is None:
            continue
        start, end = number.start(), unit.end()
        if final_start < end and final_end > start:
            if not final_start <= start < end <= final_end:
                raise ValueError('FINAL_SPLITS_NUMBER_UNIT')

