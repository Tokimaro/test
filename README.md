# Tradebot

Трейд-бот для Bybit (крипта) и фондового рынка США (Alpaca) с веб-панелью.
Детальный план и описание стратегии — в [PLAN.md](PLAN.md).

> ⚠️ Бот не гарантирует прибыль. Порядок запуска: бэктест → walk-forward → 4–8 недель
> paper-торговли → live с малым депозитом (чек-лист — раздел 15 плана).

## Что внутри

| Модуль | Что делает |
|---|---|
| `backend/app/brokers` | Bybit V5 (REST + WebSocket), Alpaca (акции), paper-брокер (симуляция на реальных данных) |
| `backend/app/market` | Поток свечей без пропусков/дублей, загрузка истории, торговые сессии NYSE |
| `backend/app/analysis` | Индикаторы (сверены с TA-Lib), мультитаймфреймовые признаки, режим рынка |
| `backend/app/strategy` | Тренд / возврат к среднему / пробой, ансамбль с уверенностью 0–100%, план SL/TP |
| `backend/app/risk` | Размер позиции, профили риска, дневные/недельные лимиты, просадка, корреляции, circuit breaker |
| `backend/app/execution`, `core` | Идемпотентные ордера, ведение позиции (безубыток, трейлинг, тайм-стоп), сверка с биржей, kill switch |
| `backend/app/backtest` | Событийный бэктест тем же кодом, метрики, walk-forward, Monte Carlo |
| `backend/app/api`, `notify` | REST/WebSocket API, вход с 2FA, Telegram-уведомления и команды |
| `frontend` | Веб-панель (React): обзор, позиции, сделки, сигналы, статистика, бэктест, настройки |

## Быстрый старт (Docker)

```bash
cp .env.example .env
# заполните: POSTGRES_PASSWORD, TB_JWT_SECRET (openssl rand -hex 32), TB_MASTER_KEY
docker compose up -d --build
docker compose exec backend python -m app.api.users create admin   # пароль + 2FA
```

Панель: http://localhost:8000 (порт слушается только на localhost — для доступа извне
используйте VPN/Tailscale или reverse-proxy с HTTPS; за прокси добавьте uvicorn
`--proxy-headers --forwarded-allow-ips=<IP прокси>`, чтобы защита от подбора пароля видела
реальные адреса клиентов).

Без ключей API бот работает в режиме **paper на реальных данных Bybit** (локальная симуляция).
С ключами testnet (`TB_BYBIT_API_KEY/SECRET`, `TB_BYBIT_TESTNET=true`) — на настоящем testnet.
Ключи mainnet в режиме paper бот не примет (движок не запустится, панель покажет ошибку) —
чтобы «тестовый» режим никогда не отправил реальные ордера.

## Типовой сценарий

```bash
cd backend
# 1. История для бэктеста (Bybit mainnet, ключи не нужны)
uv run python -m app.market.backfill --symbols BTCUSDT ETHUSDT SOLUSDT --tf 15 60 240 --days 730
# 2. Бэктест + walk-forward (или из панели, вкладка «Бэктест»)
uv run python -m app.backtest.cli --symbols BTCUSDT ETHUSDT SOLUSDT --walk-forward
# 3. Акции (нужны ключи Alpaca; в config включите markets.stocks.enabled)
uv run python -m app.market.backfill --broker alpaca --symbols AAPL MSFT --tf 15 60 D --days 1825
```

Настройки риска и стратегии меняются в панели (вкладка «Настройки») и применяются сразу;
история изменений хранится в БД. Изменение списка инструментов требует перезапуска.

## Telegram

Задайте `TB_TELEGRAM_BOT_TOKEN` (от @BotFather) и `TB_TELEGRAM_CHAT_ID`. Бот присылает сделки и
алерты и принимает команды **только из этого чата**: `/status`, `/positions`, `/pause`,
`/resume`, `/kill CONFIRM`. Если чат — группа, команды выполняются только от пользователей из
`TB_TELEGRAM_ADMIN_IDS` (например, `[123456789]`).

## Разработка

```bash
cd backend
uv sync
uv run pytest                       # тесты; с БД: TB_TEST_DATABASE_URL=postgresql+asyncpg://...
uv run ruff check . && uv run mypy app tests
TB_RUN_BOT=false uv run uvicorn app.main:create_app --factory --reload

cd ../frontend
npm install
npm run dev                         # http://localhost:5173, API проксируется на :8000
```

## Перед реальными деньгами

- ключ API **без права вывода**, с привязкой к IP сервера;
- `TB_MODE=live`, `TB_BYBIT_TESTNET=false` (для Alpaca дополнительно `TB_ALPACA_PAPER=false`);
- проверены kill switch, рестарт с открытыми позициями, обрыв сети — на testnet/paper;
- адаптеры бирж проверены автотестами на документированных ответах API, но не на живом
  подключении из среды разработки — первые сделки ведите под присмотром.
