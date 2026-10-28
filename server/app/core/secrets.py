"""Provider API key encryption at rest (V2 design §6.1, §9, §13 T7).

Master keys come only from the deployment environment and never enter the
database, Git or logs:

    SVI_SECRET_KEYS='{"k1": "<base64 32 bytes>", "k2": "..."}'
    SVI_SECRET_ACTIVE_KEY_ID=k1

Ciphertext layout: nonce (12 B) ‖ ciphertext ‖ tag (16 B); the associated
data binds a blob to its provider row and kind, so a ciphertext copied to
another row fails to decrypt.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _random
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_ENV = 'SVI_SECRET_KEYS'
ACTIVE_ENV = 'SVI_SECRET_ACTIVE_KEY_ID'


class SecretError(RuntimeError):
    """Missing/invalid master key or undecryptable blob (no secret material in text)."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    key_id: str
    last4: str


def associated_data(provider_id: str, kind: str) -> bytes:
    return f'{provider_id}|{kind}'.encode()


class SecretBox:
    def __init__(self, keys: dict[str, bytes], active_key_id: str | None):
        for key_id, key in keys.items():
            if not key_id or len(key_id) > 32 or len(key) != 32:
                raise SecretError('master keys must be 32 bytes with ids of at most 32 characters')
        if active_key_id is not None and active_key_id not in keys:
            raise SecretError('active master key id is not configured')
        self._keys = dict(keys)
        self.active_key_id = active_key_id

    @classmethod
    def from_env(cls, environ=os.environ) -> SecretBox:
        raw = environ.get(KEYS_ENV)
        if not raw:
            return cls({}, None)
        try:
            decoded = {str(k): base64.b64decode(v, validate=True) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError, AttributeError) as exc:
            raise SecretError(f'{KEYS_ENV} is not valid JSON of base64 keys') from exc
        return cls(decoded, environ.get(ACTIVE_ENV) or None)

    @property
    def available(self) -> bool:
        return self.active_key_id is not None

    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    def seal(self, plaintext: str, aad: bytes, key_id: str | None = None) -> Sealed:
        key_id = key_id or self.active_key_id
        if key_id is None or key_id not in self._keys:
            raise SecretError('no master key available for encryption')
        nonce = _random.token_bytes(12)
        blob = nonce + AESGCM(self._keys[key_id]).encrypt(nonce, plaintext.encode(), aad)
        return Sealed(blob, key_id, plaintext[-4:])

    def open(self, blob: bytes, key_id: str, aad: bytes) -> str:
        key = self._keys.get(key_id)
        if key is None:
            raise SecretError('master key for this secret is not configured')
        if len(blob) < 12 + 16:
            raise SecretError('secret ciphertext is malformed')
        try:
            return AESGCM(key).decrypt(blob[:12], blob[12:], aad).decode()
        except InvalidTag as exc:
            raise SecretError('secret ciphertext does not match its key or row') from exc


def generate_key() -> str:
    """Base64 of a fresh 32-byte master key (for operators; printed once)."""
    return base64.b64encode(_random.token_bytes(32)).decode()
