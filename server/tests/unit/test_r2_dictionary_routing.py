import pytest
from pydantic import ValidationError

from app.core.clock import loop_time
from app.core.config import Config
from app.providers import build_llm
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.routing import PurposeRoutedLlmProvider, is_local_endpoint
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.r2_dictionary import apply_aliases


def term(name, aliases, id='t1', **kw):
    return {'id': id, 'term': name, 'aliases': aliases, 'norm': name.casefold(), **kw}


@pytest.mark.parametrize('raw,candidate,terms,expected', [
    ('clowd CLowd', 'clowd、CLowd', [term('CloudKit',['clowd'])], 'CloudKit、CloudKit'),
    ('reactt', 'reactt reacttt myreactt reactt_x', [term('React',['reactt'])], 'React reacttt myreactt reactt_x'),
    ('cloud kit cloud', 'cloud kit cloud', [term('CloudKit',['cloud kit','cloud'])], 'CloudKit CloudKit'),
    # bb already names a canonical term; it cannot become another term's alias.
    ('aa bb', 'aa bb', [term('bb',['aa']),term('cc',['bb'],id='t2')], 'bb bb'),
    ('CloudKit', 'CloudKit', [term('Other',['Cloud']),term('CloudKit',[],id='t2')], 'CloudKit'),
    ('wrong', 'wrong', [term('One',['wrong']),term('Two',['wrong'],id='t2')], 'wrong'),
    ('wrong', 'wrong', [term('One',['wrong'],ambiguous_alias_norms=['wrong'])], 'wrong'),
    ('only correct', 'reactt', [term('React',['reactt'])], 'reactt'),
    ('“reactt”', '“reactt”', [term('React',['reactt'])], '“reactt”'),
    ('reactt', '“reactt” reactt', [term('React',['reactt'])], '“reactt” React'),
    ('http://reactt.test /tmp/reactt/config', 'http://reactt.test /tmp/reactt/config', [term('React',['reactt'])], 'http://reactt.test /tmp/reactt/config'),
    ('model2', 'model2', [term('model3',['model2'])], 'model2'),
    ('不要修改', '不要修改', [term('要修改',['不要修改'])], '不要修改'),
    ('IBm', 'IBm', [term('IBM-A',['IBM']),term('IBM-B',['ıBM'],id='t2')], 'IBm'),
])
def test_safe_aliases_are_frozen_nonrecursive_and_protected(raw,candidate,terms,expected):
    assert apply_aliases(raw,candidate,terms) == expected
    assert apply_aliases(raw,candidate,list(reversed(terms))) == expected


@pytest.mark.parametrize('url,allowed', [('https://api.deepseek.com',False),('https://localhost.evil.test',False),
    ('http://127.0.0.1:11434',True),('http://10.1.2.3',True),('http://[::1]:9',True),
    ('http://169.254.1.2',False),('http://user:pass@localhost',False),('http://192.168.1.8',True)])
def test_classification_endpoint_policy_is_static_and_explicit(url,allowed):
    assert is_local_endpoint(url) is allowed


def test_public_classification_config_is_rejected():
    with pytest.raises(ValidationError,match='classification_llm'):
        Config(classification_llm={'provider':'openai_compatible','base_url':'https://api.deepseek.com'})


async def test_public_r2_cannot_dispatch_correction_to_transcript_endpoint():
    p = build_llm(Config(llm={'provider':'openai_compatible', 'model_id':'deepseek-flash',
        'base_url':'https://api.deepseek.com', 'thinking':'disabled'}))
    assert isinstance(p,PurposeRoutedLlmProvider)
    try:
        # No prepare/generate HTTP: classifier is unconfigured, not main fallback.
        r = await p.generate(LlmRequest('classify','fictional',{'before':'钮','after':'键'},{'type':'object'}),loop_time()+1)
        assert r.error and r.error.code=='unavailable'
        assert p.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
    finally:
        await p.close()


async def test_local_default_keeps_one_shared_scheduler():
    p = build_llm(Config())
    try:
        assert isinstance(p,ScheduledLlmProvider)
        assert p.scheduler.slots==4
    finally:
        await p.close()


async def test_separate_local_classifier_records_its_actual_resource():
    p = build_llm(Config(llm={'provider':'openai_compatible','model_id':'deepseek-flash',
        'base_url':'https://api.deepseek.com','thinking':'disabled'}, classification_llm={'provider':'mock'}))
    assert isinstance(p,PurposeRoutedLlmProvider)
    class Recorder:
        def __init__(self): self.calls=[]
        async def started(self,scope,provider,model,purpose,*args):
            self.calls.append((provider,model,purpose));return 'fictional-call'
        async def finished(self,*args,**kwargs): pass
    from app.services.inference_context import CallScope,call_scope
    recorder=Recorder();p.recorder=recorder
    try:
        with call_scope(CallScope('fictional-user',learning_event_id='fictional-event')):
            r=await p.generate(LlmRequest('classify','fictional',{'before':'钮','after':'键'},{'type':'object'}),loop_time()+2)
        assert r.error is None and recorder.calls==[('mock','mock-llm','classify')]
        assert r.parsed['evidence']['after']=='键' and r.text is None
        assert p.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
    finally:
        await p.close()
