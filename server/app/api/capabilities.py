from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .deps import AuthContext, require_auth

router = APIRouter(prefix="/api/v1", tags=["capabilities"])


@router.get("/capabilities")
async def capabilities(request: Request, ctx: AuthContext = Depends(require_auth)):
    state = request.app.state
    config = state.config
    capacity = await state.capacity.current()
    return {
        "api_version": "v1",
        "ready": state.ready,
        "degraded": state.degraded,
        "unavailable_reason": state.unavailable_reason,
        "modes": ["dictation"],
        "audio": {
            "encoding": config.audio.encoding,
            "sample_rate_hz": config.audio.sample_rate_hz,
            "channels": config.audio.channels,
            "chunk_ms": config.audio.chunk_ms,
            "max_duration_seconds": config.audio.max_duration_seconds,
            "unacked_limit_ms": config.sessions.unacked_limit_ms,
        },
        "asr": {
            "stable_partial": bool(config.asr.stable_partial_required),
            "provider": config.asr.provider,
            "model_id": state.asr.model_id if state.asr else config.asr.model_id,
            # Limit a new session would get now (V2 §8.1); sessions keep theirs.
            "max_record_seconds": sessions._limit_seconds() if (sessions := getattr(state, "sessions", None)) else None,
            "supports_hints": bool(getattr(getattr(state.asr, "capabilities", None), "supports_hints", True)),
        },
        "llm": {
            "available": state.llm_available,
            "provider": config.llm.provider,
            "model_id": config.llm.model_id,
        },
        "limits": {
            "active_sessions_per_user": config.sessions.per_user,
            "global_sessions": capacity,
            "queued_sessions": 0,
        },
        "learning": {
            "enabled": bool(ctx.user.learning_enabled),
            "observer_sample_ms": 300,
            "observer_idle_seconds": 15,
            "observer_max_seconds": 60,
        },
        "statistics_timezone": config.statistics.timezone,
    }
