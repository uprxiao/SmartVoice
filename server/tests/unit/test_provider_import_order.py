"""Fresh interpreters expose provider cycles hidden by pytest collection order."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


SERVER_ROOT = Path(__file__).resolve().parents[2]
CHILD = textwrap.dedent("""
    import asyncio
    import importlib
    import sys

    sys.path.insert(0, sys.argv[1])
    order = sys.argv[2]
    assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)

    if order == 'validation_first':
        validation = importlib.import_module('app.services.llm_validation')
        providers = importlib.import_module('app.providers')
    elif order == 'providers_first':
        providers = importlib.import_module('app.providers')
        validation = importlib.import_module('app.services.llm_validation')
    else:
        raise AssertionError('unknown import-order fixture')

    assert callable(validation.matches_schema)
    assert callable(validation.validate_response)
    assert callable(validation.generate_validated)

    # Keep the existing factory boundary and scheduler configuration. Only the
    # local mock is constructed; no prepare, health, generate or network call.
    from app.core.config import Config

    async def check_factory():
        config = Config(
            llm={'provider': 'mock', 'max_inflight': 2, 'classify_queue_size': 3},
            sessions={'initial_max_active': 7, 'admission_ceiling': 7},
        )
        provider = providers.build_llm(config)
        try:
            # Resolve these types only AFTER the original import order and
            # factory call, so they cannot pre-initialize the failing cycle.
            from app.providers.llm.mock import MockLlmProvider
            from app.providers.llm.scheduled import ScheduledLlmProvider
            assert isinstance(provider, ScheduledLlmProvider)
            assert isinstance(provider.inner, MockLlmProvider)
            assert provider.config is config.llm
            assert provider.provider_id == provider.inner.provider_id == 'mock'
            assert provider.model_id == provider.inner.model_id
            assert provider.scheduler.slots == 2
            assert provider.scheduler.limits == {'high': 7, 'low': 3}
        finally:
            await provider.close()

    asyncio.run(check_factory())
    print('imports-and-mock-factory-ok:' + order)
""")


@pytest.mark.parametrize("order", ["validation_first", "providers_first"])
def test_validation_and_providers_import_in_a_fresh_interpreter(order, tmp_path):
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", CHILD, str(SERVER_ROOT), order],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "imports-and-mock-factory-ok:" + order
