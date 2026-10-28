"""Deterministic Unicode anchor checks; not a proof of complete semantic fidelity."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
import unicodedata

from .refiner_core import FILLER, substantive, verified_omissions

TRIGGER = re.compile(r'(?:不对|不是|改为|改用|改成|更正|说错了?|我重说|重新说|应该是|准确说|而是|i mean|rather|instead|no)', re.I)
NUMBER = re.compile(r'[+-]?\d+(?:[.:%/-]\d+)*|[零一二两三四五六七八九十百千万亿]+(?=个|次|秒|分钟|小时|天|项|条|份|台)')
PATH = re.compile(r'(?:[~/][\w.\-]+){2,}|[A-Za-z]:[\\/][^\s，。；;]+')
# Detect complete original URL literals, including bare dotted hosts. These
# spans constrain term/filler edits; ordinary English outside stays eligible.
URL = re.compile(r'(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s，。；;！？“”"<>]+'
                 r'|(?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{2,63}'
                 r'(?::[0-9]{1,5})?(?:[/?#][^\s，。；;！？“”"<>]*)?')
ENGLISH = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:[-+][A-Za-z0-9_]+)*')
NEGATION = re.compile(r'不要|不能|不可以|不需要|不允许|不得|不是|不做|不删除|不修改|不上传|不保存|禁止|未|别|\b(?:not|never|without|no)\b')
QUOTED = re.compile(r'“[^”]*”|‘[^’]*’|"(?:\\.|[^"\\])*"|`[^`]*`')


def normalized(value):
    return unicodedata.normalize('NFKC', value).casefold()


def span(raw, start, end, exact):
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw)
        or not isinstance(exact, str) or raw[start:end] != exact):
        raise ValueError('EVIDENCE_MISMATCH')
    return start, end


def _counter(pattern, text):
    return Counter(pattern.findall(text))


def protected_problem(effective, candidate):
    if not candidate.strip() or len(candidate) > min(32768, len(effective) * 2 + 120):
        return True
    # Case is preserved. Only explicit validated edit positions may exempt an
    # old spelling/value; equal token counts alone never prove full fidelity.
    if any(_counter(pattern, effective) != _counter(pattern, candidate)
           for pattern in (PATH, URL, NUMBER, ENGLISH, NEGATION)):
        return True
    # Existing literals and their multiplicities must survive a free candidate
    # even when no edit was declared. New quote formatting outside those
    # originals is not by itself a change to an original literal.
    original_quotes, actual_quotes = _counter(QUOTED, effective), _counter(QUOTED, candidate)
    return any(actual_quotes[literal] != count for literal, count in original_quotes.items())


def declared_filler_retained(raw, candidate, edits):
    """Prove a declared deletion remains, without mapping a free rewrite.

    Called after validate_edits and the protected-content check. A
    unique exact window (up to eight original codepoints on each side) must
    survive in both strings, with any original text boundary still anchored.
    Repeated/changed context is inconclusive, not a guessed source mapping.
    """
    quoted = None
    for edit in edits:
        if edit['kind'] != 'filler':
            continue
        start, end = edit['source_start'], edit['source_end']
        if not substantive(raw[start:end]):
            continue  # This check does not constrain punctuation-only layout.
        left, right = max(0, start - 8), min(len(raw), end + 8)
        if left == start and right == end:
            continue  # No adjacent context; do not infer identity from the word.
        window = raw[left:right]
        if raw.find(window) != left or raw.find(window, left + 1) != -1:
            continue
        target = candidate.find(window)
        if target == -1 or candidate.find(window, target + 1) != -1:
            continue
        if (left == 0 and target != 0) or (right == len(raw) and target + len(window) != len(candidate)):
            continue
        source_start, source_end = target + start - left, target + end - left
        if quoted is None:
            quoted = [(match.start(), match.end()) for match in QUOTED.finditer(candidate)]
        if any(source_start < qend and source_end > qstart for qstart, qend in quoted):
            continue  # Another quoted occurrence is not the spoken filler.
        return True
    return False


def _target_position(raw, candidate, start, end, replacement):
    blocks = SequenceMatcher(None, raw, candidate, autojunk=False).get_matching_blocks()
    left = [(a + min(size, start - a), b + min(size, start - a))
            for a, b, size in blocks if a < start and size]
    right = [(max(a, end), b + max(0, end - a))
             for a, b, size in blocks if a + size > end]
    candidate_start = max(left)[1] if left else 0
    candidate_end = min(right)[1] if right else len(candidate)
    if substantive(candidate[candidate_start:candidate_end]) != substantive(replacement):
        raise ValueError('TERM_POSITION_MISMATCH')


def _filler_splits_word(raw, start, end):
    source = raw[start:end]
    # fullmatch(source) sees artificial boundaries at the model's span edges.
    # Check the original text before granting an anchor-deletion exemption.
    if re.search(r"那个|就是说|就是|然后|额", source):
        if start and source[0].isalnum() and (raw[start-1].isalnum() or raw[start-1] == "_"):
            return True
        if end < len(raw) and source[-1].isalnum() and (raw[end].isalnum() or raw[end] == "_"):
            return True
    if re.search(r"[A-Za-z]", source):
        if start and re.match(r"[A-Za-z0-9_]", source[0]) and re.match(r"[A-Za-z0-9_]", raw[start-1]):
            return True
        if end < len(raw) and re.match(r"[A-Za-z0-9_]", source[-1]) and re.match(r"[A-Za-z0-9_]", raw[end]):
            return True
    return False


def validate_edits(raw, candidate, edits, terms, *, include_repetitions=False):
    changes, occupied, corrections, term_evidence, repetitions = [], [], [], [], []
    by_id = {t.get('id'): t for t in terms if isinstance(t.get('id'), str)}
    quoted = [(m.start(), m.end()) for m in QUOTED.finditer(raw)]
    # Freeze boundaries against original raw once. A short alias or punctuation
    # span must not hide its membership in a larger protected literal.
    literal_spans = [(m.start(), m.end()) for pattern in (PATH, URL, NUMBER, NEGATION)
                     for m in pattern.finditer(raw)]
    for edit in edits:
        start, end = span(raw, edit['source_start'], edit['source_end'], edit['source_text'])
        source, replacement, kind = edit['source_text'], edit['replacement_text'], edit['kind']
        if any(start < last_end and end > last_start for last_start, last_end in occupied):
            raise ValueError('OVERLAPPING_EDITS')
        if any(start < qend and end > qstart for qstart, qend in quoted) and source != replacement:
            raise ValueError('LITERAL_EDIT')
        occupied.append((start, end))
        if kind != 'self_correction' and any(key in edit for key in ('trigger', 'replacement_source_start', 'replacement_source_end')):
            raise ValueError('UNEXPECTED_CORRECTION_EVIDENCE')
        if kind != 'term_correction' and any(key in edit for key in ('dictionary_term_id', 'dictionary_term', 'dictionary_alias')):
            raise ValueError('UNEXPECTED_TERM_EVIDENCE')
        if kind == 'filler':
            if replacement or not FILLER.fullmatch(source):
                raise ValueError('INVALID_FILLER')
            if _filler_splits_word(raw, start, end):
                raise ValueError('FILLER_SPLITS_WORD')
            if any(start < last_end and end > last_start for last_start, last_end in literal_spans):
                raise ValueError('FILLER_OVERLAPS_PROTECTED_LITERAL')
            changes.append((start, end, ''))
        elif kind == 'repetition':
            # Reuse the audited local repeat guard, with an already exact span.
            verified_omissions(source, [{'kind': 'repeat', 'source': source, 'replacement': replacement}], replacement)
            if include_repetitions:
                repetitions.append({'kind': 'repetition', 'source_start': start, 'source_end': end,
                                    'source_text': source, 'replacement_text': replacement})
            changes.append((start, end, replacement))
        elif kind == 'format':
            if substantive(source) != substantive(replacement):
                raise ValueError('FORMAT_CHANGED_CONTENT')
            # Layout edits carry no exemption from original anchor protection.
            # In particular '.' -> '' inside 3.5 must not turn the guard's own
            # baseline into 35 before comparing it with the candidate.
        elif kind == 'self_correction':
            trigger = edit.get('trigger', {})
            tstart, tend = span(raw, trigger.get('start'), trigger.get('end'), trigger.get('text'))
            rstart, rend = span(raw, edit.get('replacement_source_start'), edit.get('replacement_source_end'), replacement)
            if (not TRIGGER.fullmatch(trigger['text']) or not start < tstart < tend <= rstart < rend
                or any(tstart < qend and tend > qstart for qstart, qend in quoted)):
                raise ValueError('INVALID_CORRECTION_TRIGGER')
            if end <= tstart:
                # Old-value-only span: the final value already occurs later in
                # raw. Remove only old value and its explicit trigger, never
                # duplicate the new value or erase an unrelated number.
                changes.extend([(start, end, ''), (tstart, tend, '')])
            elif end == rend:
                changes.append((start, end, replacement))
            else:
                raise ValueError('CORRECTION_SPAN_AMBIGUOUS')
            old = raw[start:min(end, tstart)]
            # Punctuation can carry a real corrected value (3.5 -> 35). Equal
            # layout-stripped text is not equality when exact anchors differ.
            same_anchors = all(_counter(pattern, old) == _counter(pattern, replacement)
                               for pattern in (PATH, URL, NUMBER, ENGLISH, NEGATION))
            if not substantive(old) or (substantive(old) == substantive(replacement) and same_anchors):
                raise ValueError('CORRECTION_HAS_NO_OLD_VALUE')
            if replacement not in candidate:
                raise ValueError('CORRECTION_FINAL_VALUE_MISSING')
            corrections.append(edit)
        elif kind == 'term_correction':
            if any(start < last_end and end > last_start for last_start, last_end in literal_spans):
                raise ValueError('TERM_OVERLAPS_PROTECTED_LITERAL')
            term = by_id.get(edit.get('dictionary_term_id'))
            alias = edit.get('dictionary_alias')
            if (term is None or edit.get('dictionary_term') != term['term'] or replacement != term['term']
                or alias not in term.get('aliases', []) or normalized(source) != normalized(alias)
                or normalized(alias) in term.get('ambiguous_alias_norms', [])):
                raise ValueError('TERM_SNAPSHOT_MISMATCH')
            owners = {t['id'] for t in terms if any(normalized(a) == normalized(alias)
                      for a in [t['term'], *t.get('aliases', [])])}
            if owners != {term['id']}:
                raise ValueError('TERM_ALIAS_AMBIGUOUS')
            if (any(_counter(pattern, source) != _counter(pattern, replacement) for pattern in (NUMBER, PATH, NEGATION))
                or PATH.search(source) or PATH.search(replacement)):
                raise ValueError('TERM_CHANGED_PROTECTED_VALUE')
            if source.isascii() and ((start and re.match(r'[A-Za-z0-9_]', raw[start-1]))
                or (end < len(raw) and re.match(r'[A-Za-z0-9_]', raw[end]))):
                raise ValueError('TERM_NOT_WHOLE_WORD')
            _target_position(raw, candidate, start, end, replacement)
            changes.append((start, end, replacement))
            term_evidence.append({**edit, 'left': raw[max(0, start-80):start], 'right': raw[end:end+80],
                                  'contexts': term.get('contexts', [])[:3]})
        else:
            raise ValueError('UNKNOWN_EDIT_KIND')
    # Self-correction trigger ranges must not silently overlap another edit.
    changes.sort()
    if any(a[1] > b[0] for a, b in zip(changes, changes[1:])):
        raise ValueError('OVERLAPPING_EDITS')
    result, cursor = [], 0
    for start, end, replacement in changes:
        result.extend([raw[cursor:start], replacement]); cursor = end
    result.append(raw[cursor:])
    if include_repetitions:
        # Returned only after every original edit, overlap and protected-span
        # validation has succeeded. This proves local syntax, not semantics.
        return ''.join(result), corrections, term_evidence, repetitions
    return ''.join(result), corrections, term_evidence


def validate_audit(raw, response):
    violations = response['violations']
    if response['decision'] == 'pass' and violations:
        raise ValueError('PASS_WITH_VIOLATIONS')
    if response['decision'] == 'reject' and not violations:
        raise ValueError('REJECT_WITHOUT_EVIDENCE')
    for item in violations:
        if (type(item['source_start']) is not int or type(item['source_end']) is not int
            or not 0 <= item['source_start'] < item['source_end'] <= len(raw)
            or not item['reason_code'].strip()):
            raise ValueError('AUDIT_EVIDENCE_INVALID')
    return response['decision'] == 'pass'
