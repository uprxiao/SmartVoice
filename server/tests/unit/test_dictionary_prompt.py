import asyncio
import json
from types import SimpleNamespace
from app.core.clock import loop_time
from app.services.dictionary_prompt import asr_hints, refine_hints
from app.providers.asr.base import AsrOpenOptions
from app.providers.asr.mlx_whisper import MlxAsrSession


def entry(index, term, aliases=(), updated=1):
    return dict(id=str(index), term=term, norm=term.lower(), aliases=list(aliases), contexts=[], updated_timestamp=updated)


def test_asr_correct_terms_are_recent_ordered_bounded_and_alias_free():
    snapshot = [entry(i, 'Word'+str(i), ['WRONG_ALIAS'], i) for i in range(150)]
    result = asr_hints(snapshot)
    assert len(result) == 100 and result[0] == 'Word149'
    assert 'WRONG_ALIAS' not in result and len(' '.join(result)) <= 2000
    assert len(' '.join(asr_hints([entry(i, 'X'*64) for i in range(100)]))) <= 2000


def test_refine_selects_raw_matches_not_creation_first_fifty_and_flags_global_alias_collision():
    snapshot = [entry(i, 'Term'+str(i)) for i in range(100)]
    snapshot += [entry(100, 'Claude', ['cloud']), entry(101, 'Claw', ['cloud'])]
    result = refine_hints(snapshot, 'Term99 和 cloud')
    assert {item['term'] for item in result} == {'Term99', 'Claude', 'Claw'}
    assert all(item['ambiguous_alias_norms'] == ['cloud'] for item in result if item['term'] in ('Claude', 'Claw'))
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 4000


def test_mlx_final_passes_hint_text_to_actual_engine_input():
    async def run():
        calls = []
        class Engine:
            model_id = 'fixture'
            async def cancel(self, *args, **kwargs):
                pass
            async def decode(self, audio, prompt, **kwargs):
                calls.append((audio, prompt, kwargs))
                return {'words': [], 'segments': [], 'seconds': 0}
        config = SimpleNamespace(step_seconds=1, trim_seconds=10)
        session = MlxAsrSession(Engine(), config, AsrOpenOptions('session', 1, hint_terms=['OpenAI', 'Claude'], user_id='user'))
        session.buffer.extend(b'\0\0' * 4000)
        await session.finish(4000, loop_time()+1)
        assert calls[0][1] == 'OpenAI Claude'
        assert calls[0][2]['purpose'] == 'asr_finish'
        assert not session.buffer
    asyncio.run(run())
