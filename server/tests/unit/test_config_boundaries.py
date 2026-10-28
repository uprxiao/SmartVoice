import unittest

from pydantic import ValidationError

from app.core.config import Config
from app.services.session_runtime import RuntimeSession, SessionManager
from unittest.mock import patch


class HardBoundaryTests(unittest.TestCase):
    def test_contract_limits_cannot_be_widened_by_yaml(self):
        invalid = [
            ("asr", "finish_timeout_ms", 10001), ("asr", "decode_timeout_ms", 3001),
            ("llm", "call_timeout_ms", 10001), ("llm", "foreground_wait_ms", 2001),
            ("llm", "classify_wait_ms", 1001), ("llm", "classify_queue_size", 21),
            ("llm", "automatic_retries_per_attempt", 2),
            ("pipeline", "stop_to_result_budget_ms", 30001),
            ("pipeline", "classification_budget_ms", 10001),
            ("retry", "audio_ttl_seconds", 601), ("retry", "max_cached_sessions", 21),
            ("sessions", "auth_poll_ms", 5001), ("sessions", "connect_timeout_ms", 10001),
            ("sessions", "audio_idle_timeout_ms", 10001), ("sessions", "unacked_limit_ms", 2001),
            ("cleanup", "audio_interval_seconds", 2), ("server", "workers", 2),
            ("auth", "argon2_memory_kib", 8192),
        ]
        for group, key, value in invalid:
            with self.subTest(group=group, key=key), self.assertRaises(ValidationError):
                Config.model_validate({group: {key: value}})

    def test_asr_retry_deadline_never_outlives_first_failure_ttl(self):
        manager = SessionManager(Config(), None, None, None, None)
        runtime = RuntimeSession("session", "user", "device", 2,
                                 end_accepted_at=100, deadline=130, retry_expires_at=1000.1)
        with patch("app.services.session_runtime.loop_time", return_value=100), \
             patch("app.services.session_runtime.time.time", return_value=1000):
            self.assertAlmostEqual(manager._asr_deadline(runtime), 100.1)
        runtime.retry_expires_at = None
        with patch("app.services.session_runtime.loop_time", return_value=100):
            self.assertEqual(manager._asr_deadline(runtime), 110)
