"""Безопасность панели (раздел 9.3 плана): argon2-пароли, TOTP 2FA, JWT, шифрование ключей."""

import secrets
import time
from typing import Any

import jwt
import pyotp
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

log = structlog.get_logger()

_hasher = PasswordHasher()
JWT_ALG = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, login: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=login, issuer_name="Tradebot")


def verify_totp(secret: str, code: str) -> bool:
    # valid_window=1 — допускаем расхождение часов на ±30 секунд
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


class TokenService:
    def __init__(self, secret: str, ttl_minutes: int) -> None:
        if not secret:
            # без заданного секрета токены живут до перезапуска процесса
            secret = secrets.token_hex(32)
            log.warning("auth.ephemeral_jwt_secret")
        self._secret = secret
        self._ttl = ttl_minutes * 60

    def issue(self, login: str) -> str:
        now = int(time.time())
        payload = {"sub": login, "iat": now, "exp": now + self._ttl}
        return jwt.encode(payload, self._secret, algorithm=JWT_ALG)

    def verify(self, token: str) -> str | None:
        try:
            payload: dict[str, Any] = jwt.decode(token, self._secret, algorithms=[JWT_ALG])
        except jwt.PyJWTError:
            return None
        sub = payload.get("sub")
        return str(sub) if sub else None


class SecretBox:
    """Шифрование секретов (API-ключей) мастер-ключом Fernet из TB_MASTER_KEY."""

    def __init__(self, master_key: str) -> None:
        self._fernet = Fernet(master_key.encode()) if master_key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("TB_MASTER_KEY не задан — хранить секреты в БД нельзя")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str | None:
        if self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            log.error("secrets.decrypt_failed")
            return None


def mask(value: str) -> str:
    return f"{value[:4]}…{value[-2:]}" if len(value) > 8 else "••••"
