"""Bind model identity to mlx-whisper 0.4.3 file choice; no neural-quality claim."""
import hashlib
import pytest
from app.services.asr_core import AsrConfig, AsrEngine


@pytest.mark.asyncio
@pytest.mark.parametrize('names,loaded', [
    (['weights.safetensors', 'weights.npz'], 'weights.safetensors'),
    (['weights.safetensors', 'model.safetensors'], 'weights.safetensors'),
    (['weights.npz', 'model.safetensors'], 'weights.npz'),
    (['weights.npz'], 'weights.npz'),
    (['weights.safetensors'], 'weights.safetensors'),
])
async def test_manifest_digest_uses_the_installed_loader_file_choice(tmp_path, names, loaded):
    config = b'{"fixture":"file binding only"}'
    (tmp_path / 'config.json').write_bytes(config)
    for name in names:
        (tmp_path / name).write_bytes(('different fixture bytes for ' + name).encode())
    expected = hashlib.sha256(config + (tmp_path / loaded).read_bytes()).hexdigest()
    engine = AsrEngine(AsrConfig(tmp_path, prewarm=False), model_sha256=expected)
    try:
        await engine.prepare()
        assert engine.model_id == expected
        assert all(worker.process is None for worker in engine.workers)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_unsupported_huggingface_filename_is_not_a_prepared_local_model(tmp_path):
    (tmp_path / 'config.json').write_bytes(b'{"fixture":"no supported weights"}')
    (tmp_path / 'model.safetensors').write_bytes(b'not loaded by mlx-whisper 0.4.3')
    engine = AsrEngine(AsrConfig(tmp_path, prewarm=False))
    try:
        with pytest.raises(ValueError, match='ASR 本地模型不完整'):
            await engine.prepare()
        assert engine.model_id == '' and all(w.process is None for w in engine.workers)
    finally:
        await engine.close()
