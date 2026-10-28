"""Unit regressions for local fence ordering, not physical worker SLA evidence."""
import asyncio
from types import SimpleNamespace

from app.services.session_runtime import SessionManager, RuntimeSession
from app.providers.asr.mlx_whisper import MlxWhisperProvider


class BlockedDatabase:
    def session_factory(self):
        return self
    async def __aenter__(self):
        await asyncio.Event().wait()
    async def __aexit__(self, *args):
        return False


def manager():
    config = SimpleNamespace(sessions=SimpleNamespace(auth_poll_ms=10, connect_timeout_ms=10000, audio_idle_timeout_ms=10000))
    return SessionManager(config, BlockedDatabase(), None, None, None)


def test_auth_database_timeout_emits_internal_error_without_waiting_for_persistence():
    async def run():
        current = manager()
        runtime = RuntimeSession('s', 'u', 'd', 1, state='refining', audio=bytearray(b'pcm'))
        current.runtimes['s'] = runtime
        await asyncio.wait_for(current._watchdog(runtime, None), 1.5)
        assert runtime.cancelled and not runtime.audio
        event = runtime.events.get_nowait()
        assert event['error']['code'] == 'INTERNAL_ERROR'
        assert current._close_code(runtime) == 1011
        for task in list(current.background_tasks):
            task.cancel()
        await asyncio.gather(*list(current.background_tasks), return_exceptions=True)
    asyncio.run(run())


def test_audio_ttl_does_not_wait_for_database_pool():
    async def run():
        current = manager()
        runtime = RuntimeSession('s', 'u', 'd', 1, state='failed', audio=bytearray(b'pcm'), retry_expires_at=100)
        current.runtimes['s'] = runtime
        assert await asyncio.wait_for(current.expire_retry_audio(100), .1) == 1
        assert not runtime.audio and runtime.retry_expires_at is None
        for task in list(current.background_tasks):
            task.cancel()
        await asyncio.gather(*list(current.background_tasks), return_exceptions=True)
    asyncio.run(run())


def test_fence_does_not_roll_back_in_memory_ready_state():
    async def run():
        current = manager()
        runtime = RuntimeSession('s', 'u', 'd', 1, state='ready')
        current._fence_runtime(runtime, 'INTERNAL_ERROR')
        assert runtime.state == 'ready' and not runtime.cancelled
        assert runtime.events.empty() and not current.background_tasks
    asyncio.run(run())


def test_successful_worker_repair_can_restore_readiness_without_new_decode():
    async def run():
        future = asyncio.get_running_loop().create_future(); future.set_result(None)
        provider = object.__new__(MlxWhisperProvider)
        provider.engine = SimpleNamespace(model_id='test', repairs={0: future}, workers=[
            SimpleNamespace(process=SimpleNamespace(is_alive=lambda: True))])
        assert await provider.health() is True
        assert provider.engine.repairs == {}
    asyncio.run(run())
