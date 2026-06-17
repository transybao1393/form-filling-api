"""RSA-OAEP transport encryption for login/signup passwords.

The browser encrypts the plaintext password with the public key before
POST /auth/login or /auth/signup. The API decrypts with the private key
before argon2 hash/verify so the raw password never appears in request logs.

Requires AUTH_RSA_PRIVATE_KEY_PEM in the environment.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import config

_private_key: rsa.RSAPrivateKey | None = None


def _load_private_key() -> rsa.RSAPrivateKey:
    global _private_key
    if _private_key is not None:
        return _private_key

    pem_raw = config.AUTH_RSA_PRIVATE_KEY_PEM
    if not pem_raw or not pem_raw.strip():
        raise RuntimeError("AUTH_RSA_PRIVATE_KEY_PEM is required")

    pem = pem_raw.replace("\\n", "\n").encode()
    loaded = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise RuntimeError("AUTH_RSA_PRIVATE_KEY_PEM must be an RSA private key")

    _private_key = loaded
    return _private_key


def public_key_pem() -> str:
    key = _load_private_key()
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def decrypt_password(encrypted_b64: str) -> str:
    """Decrypt RSA-OAEP (SHA-256) ciphertext from the browser."""
    key = _load_private_key()
    ciphertext = base64.b64decode(encrypted_b64, validate=True)
    plain = key.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plain.decode("utf-8")
