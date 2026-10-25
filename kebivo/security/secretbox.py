"""AES-256-GCM destination secrets bound to their destination row."""

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kebivo.settings import Settings


class SecretError(ValueError):
    """A blob cannot be opened with the configured keys."""


def key_id(key: bytes) -> str:
    return hmac.new(key, b"key-id", hashlib.sha256).hexdigest()[:8]


class SecretBox:
    def __init__(self, current: str, previous: str | None = None) -> None:
        self.current = bytes.fromhex(current)
        self.previous = bytes.fromhex(previous) if previous else None
        self.current_key_id = key_id(self.current)
        self.previous_key_id = key_id(self.previous) if self.previous else None

    @classmethod
    def from_settings(cls, settings: Settings) -> "SecretBox":
        return cls(settings.encryption_key, settings.encryption_key_previous)

    def encrypt(
        self, destination_id: str, secrets: dict[str, Any], *, purpose: str = "destination-secrets"
    ) -> str:
        nonce = os.urandom(12)
        payload = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode()
        aad = f"{destination_id}|{purpose}|v1".encode()
        ciphertext = AESGCM(self.current).encrypt(nonce, payload, aad)
        return ":".join(
            (
                "v1",
                self.current_key_id,
                base64.b64encode(nonce).decode(),
                base64.b64encode(ciphertext).decode(),
            )
        )

    def decrypt(
        self, destination_id: str, blob: str, *, purpose: str = "destination-secrets"
    ) -> dict[str, Any]:
        try:
            version, identifier, nonce64, ciphertext64 = blob.split(":")
            if version != "v1":
                raise ValueError
            key = {self.current_key_id: self.current}
            if self.previous is not None:
                key[key_id(self.previous)] = self.previous
            selected = key[identifier]
            nonce = base64.b64decode(nonce64, validate=True)
            ciphertext = base64.b64decode(ciphertext64, validate=True)
            if len(nonce) != 12:
                raise ValueError
            raw = AESGCM(selected).decrypt(
                nonce, ciphertext, f"{destination_id}|{purpose}|v1".encode()
            )
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError, binascii.Error, InvalidTag):
            raise SecretError("Secrets cannot be decrypted") from None


def mask(secrets: dict[str, Any]) -> dict[str, dict[str, bool]]:
    return {field: {"set": True} for field in secrets}


def encrypt(destination_id: str, secrets: dict[str, Any]) -> str:
    return SecretBox.from_settings(Settings.from_env()).encrypt(destination_id, secrets)


def decrypt(destination_id: str, blob: str) -> dict[str, Any]:
    return SecretBox.from_settings(Settings.from_env()).decrypt(destination_id, blob)
