"""Secret encryption at rest, password hashes and the signed session cookie."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "pb_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30

_fernet: Fernet | None = None
_signer: URLSafeTimedSerializer | None = None
_key: str | None = None


def init_security(data_dir: Path) -> None:
    global _fernet, _signer, _key
    key_file = data_dir / "secret.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    key = key_file.read_bytes().strip()
    _fernet = Fernet(key)
    _key = key.decode()
    _signer = URLSafeTimedSerializer(key.decode(), salt="plainboard-session")


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    assert _fernet is not None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    assert _fernet is not None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


# scrypt from the standard library: memory-hard, no further dependency. The parameters are stored
# with every hash, so they can be raised later without touching existing passwords.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        return hmac.compare_digest(_scrypt(password, base64.b64decode(salt), int(n), int(r), int(p)), base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def make_session_cookie(user_id: int, session_version: int, session_id: str) -> str:
    assert _signer is not None
    return _signer.dumps({"u": user_id, "v": session_version, "s": session_id})


def read_session_cookie(value: str | None) -> tuple[int, int, str] | None:
    """The user, session version and session a cookie was made for; None if it is not ours or too old.
    A cookie from before sessions were kept (no session) is not taken: it could not be signed out."""
    if not value or _signer is None:
        return None
    try:
        data = _signer.loads(value, max_age=SESSION_MAX_AGE)
        return int(data["u"]), int(data["v"]), str(data["s"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def sign(data: dict, purpose: str) -> str:
    """A signed value for a cookie that is not the login."""
    assert _key is not None
    return URLSafeTimedSerializer(_key, salt=f"plainboard-{purpose}").dumps(data)


def unsign(value: str | None, purpose: str, max_age: int) -> dict | None:
    if not value or _key is None:
        return None
    try:
        return URLSafeTimedSerializer(_key, salt=f"plainboard-{purpose}").loads(value, max_age=max_age)
    except BadSignature:
        return None
