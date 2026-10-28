"""Persist actual calls before dispatch. No prompts, transcripts, or audio."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert

from ..models import ModelCall, ModelUsageDaily, User
from .inference_context import CallScope
from .recovery_snapshot import RecoveryIdentity


class DatabaseCallRecorder:
    def __init__(self, db):
        self.db = db

    async def started(self, scope: CallScope, provider: str, model_id: str,
                      purpose: str, processed_audio_samples: int = 0, *,
                      provider_config_id: str | None = None) -> str:
        if scope.synthetic:
            return ""
        async with self.db.session_factory() as db:
            # Serialize the per-user call number; never hold this transaction
            # during provider execution or a scheduler wait.
            await db.scalar(select(User).where(User.id == scope.user_id).with_for_update())
            query = select(func.max(ModelCall.call_no)).where(
                ModelCall.user_id == scope.user_id,
                ModelCall.session_id == scope.session_id,
                ModelCall.attempt_no == scope.attempt_no,
                ModelCall.learning_event_id == scope.learning_event_id,
                ModelCall.purpose == purpose,
            )
            row = ModelCall(user_id=scope.user_id, session_id=scope.session_id,
                attempt_no=scope.attempt_no, learning_event_id=scope.learning_event_id,
                provider=provider, model_id=model_id, purpose=purpose,
                call_no=int(await db.scalar(query) or 0) + 1, status="running",
                processed_audio_samples=processed_audio_samples,
                provider_config_id=provider_config_id)
            db.add(row)
            await db.flush()
            call_id = row.id
            await db.commit()
            return call_id

    async def finished(self, call_id: str, status: str, error_code: str | None,
                       elapsed_ms: int, prompt_tokens: int | None = None,
                       completion_tokens: int | None = None, *, expected_user_id: str | None = None,
                       processed_audio_samples: int | None = None, segments: int | None = None,
                       reconnects: int | None = None):
        if not call_id:
            return
        async with self.db.session_factory() as db:
            # The owner lookup is a plain subquery: acquire User before Call,
            # sharing the rebuild/started fence without an inverse row lock.
            owner = select(ModelCall.user_id).where(ModelCall.id == call_id)
            if expected_user_id is not None:
                owner = owner.where(ModelCall.user_id == expected_user_id)
            owner_id = await db.scalar(select(User.id).where(
                User.id == owner.scalar_subquery(),
            ).with_for_update(of=User))
            if owner_id is None:
                return
            row = await db.scalar(select(ModelCall).where(
                ModelCall.id == call_id, ModelCall.user_id == owner_id,
            ).with_for_update())
            if row is None or row.status != "running":
                return
            row.status, row.error_code = status, error_code
            row.ended_at = datetime.now(timezone.utc)
            row.elapsed_ms = max(0, elapsed_ms)
            row.prompt_tokens, row.completion_tokens = prompt_tokens, completion_tokens
            # Streaming (cloud) ASR learns its audio total only at the end.
            if processed_audio_samples is not None:
                row.processed_audio_samples = max(0, processed_audio_samples)
            row.segments, row.reconnects = segments, reconnects
            day = row.started_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            delta = dict(calls=1, successes=int(status == "succeeded"),
                # Unknown is the remote execution outcome, not a successful
                # local request. Preserve that extra dimension and its null use.
                failures=int(status in ("failed", "cancelled", "unknown")), unknown_calls=int(status == "unknown"),
                elapsed_ms=row.elapsed_ms, processed_audio_samples=row.processed_audio_samples,
                prompt_tokens_known=prompt_tokens or 0, completion_tokens_known=completion_tokens or 0,
                prompt_tokens_unknown_calls=int(prompt_tokens is None),
                completion_tokens_unknown_calls=int(completion_tokens is None))
            statement = insert(ModelUsageDaily).values(user_id=row.user_id, date=day,
                provider=row.provider, model_id=row.model_id, purpose=row.purpose, **delta)
            statement = statement.on_conflict_do_update(
                index_elements=["user_id", "date", "provider", "model_id", "purpose"],
                set_={key: getattr(ModelUsageDaily, key) + value for key, value in delta.items()})
            await db.execute(statement)
            row.aggregated_at = row.ended_at
            await db.commit()

    async def rebuild_usage_before_recovery(self) -> int:
        """Replace the terminal-call projection during quiescent startup only.

        Invoke after the instance/schema checks and before recover_incomplete,
        provider preparation or serving traffic. The shared User fence also
        prevents a stale startup transaction from overwriting a new instance's
        completion if the separate advisory-lock connection is lost. Each user's
        projection and newly filled aggregation markers commit atomically.
        This remains startup maintenance, with no online API.
        """
        terminal = ("succeeded", "failed", "cancelled", "unknown")
        key_fields = ("date", "provider", "model_id", "purpose")
        day = func.date(func.timezone("Asia/Shanghai", ModelCall.started_at))
        totals = {
            "calls": func.count(ModelCall.id),
            "successes": func.sum(case((ModelCall.status == "succeeded", 1), else_=0)),
            "failures": func.sum(case((ModelCall.status.in_(("failed", "cancelled", "unknown")), 1), else_=0)),
            "unknown_calls": func.sum(case((ModelCall.status == "unknown", 1), else_=0)),
            "elapsed_ms": func.sum(ModelCall.elapsed_ms),
            "processed_audio_samples": func.sum(ModelCall.processed_audio_samples),
            "prompt_tokens_known": func.coalesce(func.sum(ModelCall.prompt_tokens), 0),
            "completion_tokens_known": func.coalesce(func.sum(ModelCall.completion_tokens), 0),
            "prompt_tokens_unknown_calls": func.sum(case((ModelCall.prompt_tokens.is_(None), 1), else_=0)),
            "completion_tokens_unknown_calls": func.sum(case((ModelCall.completion_tokens.is_(None), 1), else_=0)),
        }
        async with self.db.session_factory() as db:
            user_ids = list((await db.scalars(select(User.id).order_by(User.id))).all())
        rebuilt = 0
        for user_id in user_ids:
            async with self.db.session_factory() as db:
                # Same first lock as started/finished; no model call or network
                # operation runs while this projection transaction owns it.
                await db.scalar(select(User.id).where(User.id == user_id).with_for_update(of=User))
                # No source status/token/clock is inferred. Running calls are
                # excluded and remain the existing recovery path's responsibility.
                groups = (await db.execute(select(
                    day.label("date"), ModelCall.provider, ModelCall.model_id, ModelCall.purpose,
                    *(value.label(name) for name, value in totals.items()),
                ).where(ModelCall.user_id == user_id, ModelCall.status.in_(terminal))
                  .group_by(day, ModelCall.provider, ModelCall.model_id, ModelCall.purpose))).mappings().all()
                expected = {
                    tuple(row[name] for name in key_fields): {name: int(row[name]) for name in totals}
                    for row in groups
                }
                keys = {tuple(row) for row in (await db.execute(select(
                    *(getattr(ModelUsageDaily, name) for name in key_fields),
                ).where(ModelUsageDaily.user_id == user_id))).all()}
                keys.update(expected)
                for key in sorted(keys):
                    values = expected.get(key, dict.fromkeys(totals, 0))
                    statement = insert(ModelUsageDaily).values(
                        user_id=user_id, **dict(zip(key_fields, key)), **values,
                    )
                    await db.execute(statement.on_conflict_do_update(
                        index_elements=["user_id", *key_fields],
                        set_={name: statement.excluded[name] for name in totals},
                    ))
                # aggregated_at is the aggregation time, not provider completion.
                # Preserve any existing marker; fill legacy terminal nulls only
                # in the very transaction that persisted their exact projection.
                await db.execute(update(ModelCall).where(
                    ModelCall.user_id == user_id, ModelCall.status.in_(terminal),
                    ModelCall.aggregated_at.is_(None),
                ).values(aggregated_at=datetime.now(timezone.utc)))
                await db.commit()
                rebuilt += len(keys)
        return rebuilt

    async def recover_incomplete(self, *, identities=None) -> int:
        """Run after the single-instance lock, before serving traffic.

        A restart cannot infer provider completion, token use, or elapsed model
        time. Preserve unknown fields; aggregate each previously running row once.
        """
        if identities is None:
            async with self.db.session_factory() as db:
                identities = tuple(RecoveryIdentity(*row) for row in (await db.execute(
                    select(ModelCall.user_id, ModelCall.id).where(ModelCall.status == "running")
                )).all())
        selected = sorted(set(identities))
        for identity in selected:
            # finished() still checks running under User -> Call locks. Exact
            # identity excludes new successor calls, including the same user.
            await self.finished(identity.resource_id, "unknown", "SERVER_RESTARTED", 0,
                                expected_user_id=identity.user_id)
        return len(selected)
