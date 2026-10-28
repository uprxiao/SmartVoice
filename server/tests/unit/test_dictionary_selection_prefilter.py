"""Preserve old literal/Unicode/boundary semantics while avoiding cold regex work."""
import copy
import random
import re
import string

import pytest

from app.services import dictionary_prompt as subject


def row(i, term, aliases=()):
    return dict(id=str(i), term=term, norm=subject.norm(term), aliases=list(aliases),
                contexts=[], updated_timestamp=i)


@pytest.mark.parametrize('raw,candidates',[
    ('请打开DOCKER和TypeScript。', ['docker','TypeScript','DOCK','项目']),
    ('dockerized _docker docker_ xdocker docker2', ['docker','DOCKER']),
    ('docker。dockerized', ['docker','dockerized']),
    ('İstanbul ıstanbul ſerver Kubernetes', ['Istanbul','server','Kubernetes','stanbul']),
    ('İDocker dockerſ Kdocker', ['Docker','docker']),
    ('Straße STRASSE straße', ['Straße','strasse','SS','ß']),
    ('Σ σ ς µ Μ μ ﬀ ff', ['Σ','σ','ς','µ','Μ','μ','ﬀ','ff']),
    ('ＡＩ AI ai', ['ＡＩ','AI','ai']),
    ('C++ C# .NET [abc] a.b foo\\bar', ['C++','C#','.NET','[abc]','a.b','foo\\bar']),
    ('项目甲和项目乙；🙂 Alpha\nBeta', ['项目甲','项目','🙂','Alpha\nBeta']),
    ('xk8s k8s k8s_ K8S', ['k8s','K8S','8s']),
    ('SS ff ffi', ['ß','ﬀ','ﬃ']),
    ('İIı i\u0307', ['İ','I','ı','i\u0307']),
    ('', ['a','词','🙂']),
])
def test_selector_equals_original_regex_for_literal_and_unicode_cases(raw,candidates):
    snapshot=[row(i,word) for i,word in enumerate(candidates)]
    expected={e['id'] for e in snapshot if subject._matches(raw,e['term'])}
    assert {e['id'] for e in subject.refine_hints(snapshot,raw)}==expected


def test_random_ascii_terms_equal_unfiltered_selector_including_global_ambiguity(monkeypatch):
    # Compare every output field against the original regex-only selector,
    # including ordering/budget and ownership from non-selected entries.
    rng=random.Random(9417)
    words=[''.join(rng.choices(string.ascii_letters+string.digits+'_.+-',k=8)) for _ in range(160)]
    snapshot=[row(i,word,[words[(i+1)%160],'shared']) for i,word in enumerate(words)]
    raw='请检查 '+ ' / '.join(words[::13])+ ' shared'
    before=copy.deepcopy(snapshot)
    result=subject.refine_hints(snapshot,raw)
    actual_matches=subject._matches
    monkeypatch.setattr(subject, '_matches', lambda text,value: actual_matches(raw,value))
    # All candidates occur in this dummy search corpus, so the prefilter passes
    # them; the captured original regex still tests only the real transcript.
    assert result==subject.refine_hints(snapshot,' '.join(words)+' shared')
    assert snapshot==before
    assert all('shared' in e['ambiguous_alias_norms'] for e in result)


@pytest.mark.parametrize('kind', ['ascii','chinese','chinese-long'])
def test_absent_dictionary_avoids_regex_compilation(monkeypatch,kind):
    def names(i):
        if kind == 'ascii':
            return f'ProjectTerm{i:04d}',[f'ProjectAlias{i:04d}x{a:02d}' for a in range(10)]
        prefix='X'*56 if kind == 'chinese-long' else ''
        alias_prefix='Y'*54 if kind == 'chinese-long' else ''
        return f'项目术语{prefix}{i:04d}',[f'项目别名{alias_prefix}{i:04d}{a:02d}' for a in range(10)]
    snapshot=[row(i,*names(i)) for i in range(5000)]
    calls=[]
    actual=subject._matches
    def observed(*args):
        calls.append(1)
        return actual(*args)
    monkeypatch.setattr(subject,'_matches',observed)
    assert subject.refine_hints(snapshot,'请整理今天的项目进展。'*8)==[]
    assert calls==[], '55000 absent candidates must not construct regexes'
