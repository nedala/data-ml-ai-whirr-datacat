"""Password-based authenticated encryption: scrypt KDF + AES-256-GCM.

Shared by the vault (data/vault/vault.json) and configuration backups
(*.bspconfig). Envelope fields: kdf, n, r, p, salt, nonce, ct (base64)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1


class DecryptError(ValueError):
    """Wrong password or tampered ciphertext."""


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode())


def derive_key(password: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                          maxmem=256 * n * r + (1 << 20), dklen=32)


def encrypt_with_key(key: bytes, salt: bytes, plaintext: bytes, aad: bytes | None = None,
                     n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> dict[str, Any]:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {"kdf": "scrypt", "n": n, "r": r, "p": p,
            "salt": b64e(salt), "nonce": b64e(nonce), "ct": b64e(ct)}


def seal(password: str, plaintext: bytes, aad: bytes | None = None) -> dict[str, Any]:
    salt = os.urandom(16)
    return encrypt_with_key(derive_key(password, salt), salt, plaintext, aad)


def envelope_key(envelope: dict[str, Any], password: str) -> bytes:
    if envelope.get("kdf") != "scrypt":
        raise DecryptError(f"unsupported kdf {envelope.get('kdf')!r}")
    return derive_key(password, b64d(envelope["salt"]), int(envelope["n"]),
                      int(envelope["r"]), int(envelope["p"]))


def decrypt_with_key(envelope: dict[str, Any], key: bytes, aad: bytes | None = None) -> bytes:
    try:
        return AESGCM(key).decrypt(b64d(envelope["nonce"]), b64d(envelope["ct"]), aad)
    except (InvalidTag, KeyError, ValueError) as exc:
        raise DecryptError("wrong passphrase or corrupted data") from exc


def unseal(envelope: dict[str, Any], password: str, aad: bytes | None = None) -> bytes:
    return decrypt_with_key(envelope, envelope_key(envelope, password), aad)


def same_key(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)
