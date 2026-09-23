"""Управление пользователями панели.

uv run python -m app.api.users create admin          # пароль + 2FA (рекомендуется)
uv run python -m app.api.users create admin --no-2fa
uv run python -m app.api.users passwd admin
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.security import hash_password, new_totp_secret, totp_uri
from app.config import get_settings
from app.db.models import UserRow
from app.db.session import make_engine, make_sessionmaker

MIN_PASSWORD = 10


def _ask_password() -> str:
    pw = getpass.getpass("Пароль: ")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    if getpass.getpass("Повторите пароль: ") != pw:
        sys.exit("Пароли не совпадают")
    return pw


async def create_user(
    sm: async_sessionmaker[AsyncSession], login: str, password: str, with_2fa: bool
) -> str | None:
    """Создаёт пользователя; возвращает otpauth-URI для приложения-аутентификатора."""
    secret = new_totp_secret() if with_2fa else None
    async with sm() as s, s.begin():
        if await s.scalar(select(UserRow).where(UserRow.login == login)):
            raise ValueError(f"пользователь {login} уже существует")
        s.add(UserRow(login=login, password_hash=hash_password(password), totp_secret=secret))
    return totp_uri(secret, login) if secret else None


async def set_password(sm: async_sessionmaker[AsyncSession], login: str, password: str) -> None:
    async with sm() as s, s.begin():
        user = await s.scalar(select(UserRow).where(UserRow.login == login))
        if user is None:
            raise ValueError(f"пользователь {login} не найден")
        user.password_hash = hash_password(password)


async def main_async(args: argparse.Namespace) -> None:
    engine = make_engine(get_settings().database_url)
    sm = make_sessionmaker(engine)
    try:
        if args.command == "create":
            uri = await create_user(sm, args.login, _ask_password(), not args.no_2fa)
            print(f"Пользователь {args.login} создан.")
            if uri:
                print("Добавьте в Google Authenticator / Aegis / 1Password (ссылка или QR из неё):")
                print(uri)
        elif args.command == "passwd":
            await set_password(sm, args.login, _ask_password())
            print("Пароль изменён.")
    except ValueError as exc:
        sys.exit(str(exc))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("login")
    c.add_argument("--no-2fa", action="store_true")
    p = sub.add_parser("passwd")
    p.add_argument("login")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
