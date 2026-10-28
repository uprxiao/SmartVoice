from __future__ import annotations

from pathlib import Path

from ..core.config import Config
from .asr.base import ProviderError as AsrProviderError
from .asr.mock import MockAsrProvider
from .llm.mock import MockLlmProvider
from .llm.ollama import OllamaProvider
from .llm.openai_compatible import OpenAICompatibleProvider


def build_egress(config: Config):
    from ..core.egress import EgressPolicy
    return EgressPolicy(config.security.egress_policy, config.security.egress_allowlist)


def build_bailian(base_url: str, api_key: str | None, model_id: str, params: dict, egress, *,
                  provider_id: str = "yaml", provider_version: str = "0"):
    from .asr.bailian_realtime import BailianRealtimeConfig, BailianRealtimeProvider
    from .params import BailianRealtimeParams
    if not base_url or not api_key:
        raise AsrProviderError("unavailable", "cloud ASR requires an address and an API key")
    values = BailianRealtimeParams.model_validate(params).model_dump()
    return BailianRealtimeProvider(BailianRealtimeConfig(
        base_url=base_url, api_key=api_key, model_id=model_id or "qwen3-asr-flash-realtime",
        provider_id=provider_id, provider_version=provider_version, **values), egress=egress)


def build_asr(config: Config):
    provider = config.asr.provider
    if provider == "bailian_realtime":
        params = dict(config.asr.params)
        params.setdefault("language", config.asr.language)
        return build_bailian(config.asr.remote_base_url or "", config.asr_api_key, config.asr.model_id,
                             params, build_egress(config))
    if config.asr.remote_base_url:
        raise AsrProviderError("unavailable", "remote/public ASR address unsupported by local ASR provider")
    if provider == "mock":
        return MockAsrProvider()
    if provider == "qwen3_mlx":
        from .asr.qwen3_mlx import Qwen3MlxProvider
        return Qwen3MlxProvider(config.asr, queue_limit=config.effective_admission_ceiling)
    if provider == "mlx_whisper":
        from ..services.asr_core import AsrConfig
        from .asr.mlx_whisper import MlxWhisperProvider

        asr_config = AsrConfig(
            model_dir=Path(config.asr.model_path),
            language=config.asr.language,
            step_seconds=config.asr.decode_interval_ms / 1000,
            trim_seconds=config.asr.decode_window_ms / 1000,
        )
        return MlxWhisperProvider(asr_config, config.asr.model_id or Path(config.asr.model_path).name,
            slots=config.asr.execution_slots, queue_limit=config.effective_admission_ceiling,
            decode_timeout_ms=config.asr.decode_timeout_ms, model_sha256=config.asr.model_sha256)
    raise AsrProviderError("unavailable", f"unsupported ASR provider: {provider}")


def _build_llm(config: Config, llm_config=None, api_key=None):
    c = llm_config or config.llm
    api_key = config.llm_api_key if llm_config is None else api_key
    provider = c.provider
    if provider == "mock":
        return MockLlmProvider()
    if provider == "ollama":
        return OllamaProvider(
            base_url=c.base_url or "http://127.0.0.1:11434",
            model_id=c.model_id,
        )
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(
            base_url=c.base_url,
            model_id=c.model_id,
            api_key=api_key,
            structured_output=c.structured_output,
            thinking=c.thinking,
            text_probe=c.refine_protocol == 'ds_r2' and llm_config is None,
        )
    raise AsrProviderError("unavailable", f"unsupported LLM provider: {provider}")


def build_llm(config: Config, *, inner=None, llm_config=None, provider_config_id: str | None = None):
    """Build the refine LLM stack.

    ``inner``/``llm_config`` replace the YAML refine provider with an
    admin-managed one (V2 §4.5); the classification route stays YAML-only and
    the public-R2 routing rule applies to either source.
    """
    # Validation imports provider base types before its functions are defined.
    from .llm.scheduled import ScheduledLlmProvider

    from .llm.routing import PurposeRoutedLlmProvider, is_local_endpoint
    refine_config = llm_config or config.llm
    primary = ScheduledLlmProvider(inner if inner is not None else _build_llm(config),
                                   refine_config, config.effective_admission_ceiling)
    primary.provider_config_id = provider_config_id
    if inner is not None:
        if config.classification_llm is not None:
            c = config.classification_llm
            classifier = ScheduledLlmProvider(_build_llm(config, c, config.classification_llm_api_key),
                                             c, config.effective_admission_ceiling)
            return PurposeRoutedLlmProvider(primary, classifier)
        public = refine_config.provider == 'openai_compatible' and not is_local_endpoint(refine_config.base_url)
        return PurposeRoutedLlmProvider(primary) if public else primary
    if config.classification_llm is not None:
        c = config.classification_llm
        same_resource = (c.provider, c.base_url.rstrip('/'), c.model_id, c.api_key_env) == (
            config.llm.provider, config.llm.base_url.rstrip('/'), config.llm.model_id, config.llm.api_key_env)
        if same_resource:
            # One physical resource must not gain a second independent permit
            # pool merely by declaring it again under a different purpose.
            if c.model_dump() != config.llm.model_dump():
                raise AsrProviderError('unavailable', 'shared LLM resource requires identical pool configuration')
            return primary
        classifier = ScheduledLlmProvider(_build_llm(config, c, config.classification_llm_api_key),
                                         c, config.effective_admission_ceiling)
        return PurposeRoutedLlmProvider(primary, classifier)
    public_r2 = (config.llm.refine_protocol == 'ds_r2' and config.llm.provider == 'openai_compatible'
                 and not is_local_endpoint(config.llm.base_url))
    # Public transcript permission does not authorize derived correction data.
    return PurposeRoutedLlmProvider(primary) if public_r2 else primary
