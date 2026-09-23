# Tradebot

Трейд-бот для Bybit (крипта + акции) с веб-панелью. Детальный план — в [PLAN.md](PLAN.md).

## Быстрый старт (Docker)

```bash
cp .env.example .env        # заполните ключи testnet
docker compose up -d --build
curl http://localhost:8000/api/health
```

## Разработка backend

```bash
cd backend
uv sync                     # Python 3.12 + зависимости
uv run pytest               # тесты
uv run ruff check . && uv run mypy app tests
uv run uvicorn app.main:create_app --factory --reload
```

Тесты с БД запускаются, если задана `TB_TEST_DATABASE_URL`
(например, `postgresql+asyncpg://tradebot:tradebot@localhost:5432/tradebot_test`).

> ⚠️ По умолчанию бот работает в режиме `paper` на testnet. Для `live` нужны ключи mainnet
> без права вывода и прохождение чек-листа из раздела 15 плана.
