"""Actual TCP adapter + startup/PG, invented generation, not real model quality."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func,select
import yaml

from app import main
from app.models import ModelCall,ModelUsageDaily
from tests.conftest import SERVER_ROOT,TEST_URL,wait_for_readiness
from tests.integration.test_llm_http_adapter_contract import Upstream,MODEL


@pytest.mark.parametrize('bad_reply',[{'done':True},{'done':True,'done_reason':'length','response':'{"ready":true}'}])
def test_load_only_or_truncated_warmup_stays_degraded_then_recovers(clean_database,monkeypatch,tmp_path,bad_reply):
    upstream=Upstream('ollama');upstream.generation_reply=bad_reply;upstream.start()
    try:
        for key,value in {'SVI_DATABASE_URL':TEST_URL,'SVI_INSTANCE_LOCK':'0','SVI_ENVIRONMENT':'development',
            'SVI_ASR_PROVIDER':'mock','SVI_LLM_PROVIDER':'ollama','SVI_LLM_BASE_URL':upstream.url,'SVI_LLM_MODEL_ID':MODEL}.items():
            monkeypatch.setenv(key,value)
        payload=yaml.safe_load((SERVER_ROOT/'config/app.yaml').read_text())
        payload['llm'].update(provider='ollama',model_id=MODEL,base_url=upstream.url)
        config=tmp_path/'generation-health.yaml';config.write_text(yaml.safe_dump(payload));monkeypatch.setenv('SVI_CONFIG',str(config))
        app=main.create_app(str(config))
        with TestClient(app) as client:
            wait_for_readiness(client)
            async def finish_current_prepare():
                async with asyncio.timeout(2):
                    while app.state.llm_prepare_task is None:await asyncio.sleep(.01)
                    await asyncio.shield(app.state.llm_prepare_task)
            client.portal.call(finish_current_prepare)
            assert not app.state.llm_prepared and not app.state.llm_available
            health=client.get('/healthz').json()
            assert health['ready'] is True and health['degraded'] is True
            assert upstream.generation_requests and not upstream.records and not upstream.errors
            upstream.generation_reply={'done':True,'done_reason':'stop','response':'{"ready":true}'}
            async def recover():
                await main._refresh_readiness(app)
                await asyncio.shield(app.state.llm_prepare_task)
                await main._refresh_readiness(app)
            client.portal.call(recover)
            assert app.state.llm_prepared and app.state.llm_available
            assert client.get('/healthz').json()['degraded'] is False
            async def calls():
                async with app.state.db.session_factory() as db:
                    return [await db.scalar(select(func.count()).select_from(model)) for model in (ModelCall,ModelUsageDaily)]
            assert client.portal.call(calls)==[0,0]
            assert not upstream.records and not upstream.errors
    finally:upstream.close()
