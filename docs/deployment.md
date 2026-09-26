# Развёртывание на сервере с нуля

Инструкция ведёт от чистого VPS до работающего бота: сначала paper, затем testnet, затем live.
Все команды — для Ubuntu 24.04 LTS (на 22.04 и Debian 12 они такие же).

## 1. Требования

### Сервер

| | Минимум | Рекомендуется | Почему |
|---|---|---|---|
| CPU | 1 vCPU | 2 vCPU | бот считает раз в день, нагрузка почти нулевая; CPU нужен для сборки образа |
| RAM | 2 ГБ + 2 ГБ swap | 4 ГБ | сборка панели (npm) требует ~1 ГБ; в работе бот + PostgreSQL занимают ~0.5–0.8 ГБ |
| Диск | 20 ГБ SSD | 40 ГБ SSD | образы Docker ~3 ГБ, база с дневными свечами — десятки МБ, остальное — логи и бэкапы |
| ОС | Ubuntu 22.04 / 24.04 LTS, Debian 12 (x86_64 или arm64) | Ubuntu 24.04 LTS | нужен Docker Engine 24+ с плагином compose |
| Сеть | постоянный публичный IPv4 | статический IPv4 | к IP привязывается API-ключ Bybit |
| Аптайм | обычный VPS | VPS с SLA 99.9% | бот торгует раз в неделю, короткий простой не опасен: после рестарта он сверяется с биржей |

Подойдёт любой VPS за $5–10 в месяц (Hetzner, DigitalOcean, Vultr, AWS Lightsail и т. п.).
Домашний компьютер не подходит: нужен постоянный IP и работа 24/7.

### Где расположить сервер

* **Страна должна быть разрешена Bybit.** По
  [списку Bybit](https://www.bybit.com/en/help-center/article/Service-Restricted-Countries)
  (сентябрь 2026) сервис недоступен в США, материковом Китае, Гонконге, Сингапуре, Канаде,
  Узбекистане, КНДР, Иране, на Кубе, в Судане, Сирии и на подконтрольных России территориях
  Украины; список меняется — проверьте перед арендой. Запрос с сервера из запрещённой страны
  вернёт ошибку доступа. Проверка после установки — шаг 7.
* **Сервер в России не рекомендуется**, даже если аккаунт Bybit оформлен на резидента РФ: сайт и
  API Bybit в России работают нестабильно (блокировки, режим «белых списков»). Надёжнее VPS
  в ЕС (Финляндия, Нидерланды, Германия) или в Азии вне списка выше.
* Задержка не критична (бот торгует раз в неделю), но разумнее Европа или Азия.
* Сервер должен достукиваться до `api.bybit.com`, `stream.bybit.com`, `api-testnet.bybit.com`,
  `stream-testnet.bybit.com`, `api.telegram.org`, GitHub и Docker Hub.

### Что ещё понадобится

* аккаунт Bybit с пройденной верификацией; для реальной торговли — **отдельный субаккаунт** под бота;
* аккаунт на [testnet.bybit.com](https://testnet.bybit.com) для этапа testnet;
* Telegram-бот (токен от @BotFather) — для уведомлений и команд;
* приложение-аутентификатор (Google Authenticator, Aegis, 1Password) — вход в панель с 2FA;
* по желанию — домен, если нужна панель по HTTPS без VPN (шаг 9, вариант В).

## 2. Первичная настройка сервера

Под root по SSH:

```bash
apt update && apt -y full-upgrade
timedatectl set-timezone UTC

# отдельный пользователь
adduser deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/      # если вход по ключу уже настроен
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
```

С вашего компьютера (если ключа ещё нет): `ssh-keygen -t ed25519`, затем
`ssh-copy-id deploy@<IP сервера>`. Проверьте вход `ssh deploy@<IP>` **до** следующего шага.

Запрет входа по паролю и под root — в `/etc/ssh/sshd_config`:

```
PermitRootLogin no
PasswordAuthentication no
```

```bash
sudo systemctl restart ssh
```

Файрвол, защита от перебора, автообновления безопасности, точное время:

```bash
sudo apt -y install ufw fail2ban unattended-upgrades chrony git
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
# sudo ufw allow 80,443/tcp        # только для варианта В (HTTPS-панель), шаг 9
sudo ufw enable
sudo dpkg-reconfigure -plow unattended-upgrades
sudo systemctl enable --now chrony fail2ban
chronyc tracking                    # System time: отклонение должно быть < 0.1 с
```

Точное время обязательно: Bybit отклоняет запросы, если часы расходятся с биржей больше чем
на окно `recv_window` (5 секунд).

Swap, если RAM 2 ГБ:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Docker

Официальный репозиторий Docker (пакет `docker.io` из Ubuntu бывает устаревшим):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy      # перелогиньтесь, чтобы группа применилась
```

Ограничение размера логов контейнеров (по умолчанию Docker хранит их без ограничений):

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }
EOF
sudo systemctl restart docker
docker compose version              # v2.x
```

> Docker публикует порты в обход ufw. В этом проекте порты БД и панели привязаны к
> `127.0.0.1`, поэтому снаружи они недоступны. Не меняйте привязку на `0.0.0.0`.

## 4. Код и секреты

```bash
sudo mkdir -p /opt/tradebot && sudo chown deploy:deploy /opt/tradebot
git clone https://github.com/Tokimaro/test.git /opt/tradebot
cd /opt/tradebot
git checkout main
cp .env.example .env
chmod 600 .env
```

Если репозиторий приватный, для клонирования нужен доступ: deploy key (`ssh-keygen -t ed25519`,
публичный ключ — в Settings → Deploy keys репозитория, только чтение; клонировать по
`git@github.com:Tokimaro/test.git`) или персональный токен с правом чтения.

Сгенерируйте секреты:

```bash
openssl rand -hex 24                # → POSTGRES_PASSWORD
openssl rand -hex 32                # → TB_JWT_SECRET (не короче 32 символов)
docker run --rm python:3.12-slim sh -c \
  "pip -q install cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
                                    # → TB_MASTER_KEY
```

Заполните `.env` (первый запуск — paper без ключей):

```ini
TB_MODE=paper
TB_LOG_LEVEL=INFO
TB_LOG_JSON=true
POSTGRES_PASSWORD=<сгенерированный>
TB_BYBIT_TESTNET=true
TB_BYBIT_API_KEY=
TB_BYBIT_API_SECRET=
TB_JWT_SECRET=<сгенерированный>
TB_MASTER_KEY=<сгенерированный>
TB_TELEGRAM_BOT_TOKEN=<токен от @BotFather>
TB_TELEGRAM_CHAT_ID=<id чата>
# TB_TELEGRAM_ADMIN_IDS=[123456789]   # если чат — группа
```

Необязательные переменные: `TB_PAPER_INITIAL_EQUITY` (стартовый капитал paper, по умолчанию
10000 USDT), `TB_JWT_TTL_MINUTES` (время сессии панели, 60), `NPM_REGISTRY` (зеркало npm для
сборки панели, если `registry.npmjs.org` недоступен — см. «Типичные проблемы»).

**Сохраните копию `.env` в менеджере паролей.** Без `TB_MASTER_KEY` ключи Bybit, сохранённые
через панель, расшифровать нельзя; `POSTGRES_PASSWORD` задаётся базе при первом создании и потом
в `.env` не меняется.

`TB_TELEGRAM_CHAT_ID`: напишите своему боту любое сообщение и откройте
`https://api.telegram.org/bot<ТОКЕН>/getUpdates` — число в `"chat":{"id":...}`.

## 5. Запуск

```bash
cd /opt/tradebot
docker compose up -d --build        # первая сборка 3–10 минут
docker compose ps                   # оба сервиса healthy
docker compose logs -f backend      # миграции БД применяются автоматически при старте
curl -s http://127.0.0.1:8000/api/health
```

Контейнеры с `restart: unless-stopped` поднимаются сами после перезагрузки сервера и падений.

## 6. Пользователь панели

```bash
docker compose exec backend python -m app.api.users create admin
```

Команда спросит пароль и покажет секрет 2FA — добавьте его в приложение-аутентификатор.
Сменить пароль: `docker compose exec backend python -m app.api.users passwd admin`.

## 7. Проверка связи с Bybit

```bash
docker compose exec backend python -c "import urllib.request as u; print(u.urlopen('https://api.bybit.com/v5/market/time').read())"
```

Ответ с `"retCode":0` — биржа доступна. Ошибка 403 или сообщение о регионе — страна сервера
запрещена, нужен другой сервер. Затем проверьте, что свечи идут: `docker compose logs backend`
без ошибок, в панели на «Обзоре» обновляются цены.

## 8. Бэктест на сервере (по желанию)

```bash
docker compose exec backend python -m app.market.backfill \
  --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT TRXUSDT BCHUSDT --days 1500
docker compose exec backend python -m app.backtest.cli \
  --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT TRXUSDT BCHUSDT
```

Или вкладка «Бэктест» в панели.

## 9. Доступ к панели

Панель слушает только `127.0.0.1:8000` сервера. Три способа, от самого безопасного:

**А. SSH-туннель** (ничего не надо открывать):

```bash
ssh -N -L 8000:127.0.0.1:8000 deploy@<IP сервера>
# в браузере: http://localhost:8000
```

**Б. Tailscale / WireGuard** — панель видна только в вашей приватной сети:

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 8000      # https://<имя-сервера>.<tailnet>.ts.net только для вашей сети
```

**В. Публичный HTTPS через Caddy** (нужен домен с A-записью на IP сервера и открытые 80/443):

```bash
sudo apt -y install caddy
sudo tee /etc/caddy/Caddyfile <<'EOF'
bot.example.com {
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy         # сертификат Let's Encrypt выпускается автоматически
```

Чтобы защита от подбора пароля видела реальные IP клиентов, а не адрес прокси, создайте
`/opt/tradebot/docker-compose.override.yml` (compose подхватывает его сам):

```yaml
services:
  backend:
    command: ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0",
              "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

`*` здесь допустимо, потому что порт 8000 доступен только с самого сервера (Caddy перезаписывает
`X-Forwarded-For` реальным адресом клиента). Затем `docker compose up -d`.
Панель в интернете — это вход в торговый счёт: сложный пароль, 2FA обязательно; по возможности
предпочтите варианты А или Б.

## 10. Этапы запуска

### Этап 1. Paper на реальных ценах — 4–8 недель

Настройки из шага 4 (`TB_MODE=paper`, ключей нет). Бот берёт настоящие цены Bybit и исполняет
ордера в симуляции. Первый расчёт после старта сразу собирает портфель, дальше — ребалансировка
по понедельникам после 00:00 UTC. Проверьте: приходят уведомления Telegram, работают `/status`,
`/pause`, `/resume`; во вкладке «Доли» каждый день появляется запись; после
`docker compose restart backend` бот продолжает с того же места.

### Этап 2. Testnet Bybit — 2–4 недели

1. На [testnet.bybit.com](https://testnet.bybit.com) получите тестовые USDT и создайте API-ключ
   (права: Spot — торговля; без вывода).
2. В `.env`: `TB_MODE=paper`, `TB_BYBIT_TESTNET=true`, ключи в `TB_BYBIT_API_KEY/SECRET`
   (или через панель «Настройки» → ключи API; ключи из `.env` имеют приоритет).
3. `docker compose up -d`, дождаться первой ребалансировки, сверить сделки в панели с историей
   ордеров на testnet.
4. Проверить аварийные сценарии: `/kill CONFIRM` (всё продаётся в USDT, бот встаёт на паузу),
   рестарт контейнера с монетами в портфеле, кратковременное отключение сети
   (`sudo ufw deny out 443` на минуту, потом `sudo ufw delete deny out 443`).

### Этап 3. Live — малым депозитом

1. На Bybit создайте **субаккаунт** только под бота и переведите туда стартовую сумму в USDT.
   Капитал стратегии — свободные USDT плюс монеты из списка на этом счёте.
2. API-ключ субаккаунта: тип «системный», права **только Spot — Trade**, **без Withdraw и
   Transfer**, привязка к IP сервера (`curl -4 ifconfig.me`).
3. В `.env`:

   ```ini
   TB_MODE=live
   TB_BYBIT_TESTNET=false
   TB_BYBIT_API_KEY=<ключ>
   TB_BYBIT_API_SECRET=<секрет>
   ```

   В режиме paper ключи mainnet бот отвергает — это защита от случайной реальной торговли.
4. `docker compose up -d`. **Сразу после старта бот соберёт портфель рыночными ордерами** —
   запускайте, когда готовы к покупкам. Первые дни — под присмотром.
5. Риск-профиль и остановку по просадке (`max_drawdown_stop_pct`) задайте в панели,
   «Настройки».

## 11. Резервные копии

База хранит историю сделок, настройки, пользователей и зашифрованные ключи. Ежедневный дамп:

```bash
mkdir -p /opt/tradebot-backups
crontab -e
```

```
15 3 * * * cd /opt/tradebot && docker compose exec -T db pg_dump -U tradebot -Fc tradebot > /opt/tradebot-backups/tradebot-$(date +\%F).dump && find /opt/tradebot-backups -name '*.dump' -mtime +14 -delete
```

Копируйте дампы за пределы сервера (rclone/restic в S3, Backblaze B2 и т. п.). Копия `.env`
хранится отдельно (шаг 4).

Восстановление:

```bash
docker compose stop backend
docker compose exec -T db pg_restore -U tradebot -d tradebot --clean --if-exists < tradebot-YYYY-MM-DD.dump
docker compose start backend
```

После восстановления бот сверит владения с кошельком биржи.

## 12. Обновление и откат

```bash
cd /opt/tradebot
# 1. бэкап (команда pg_dump из шага 11)
git fetch && git log --oneline HEAD..@{u}          # что изменится
git pull
docker compose up -d --build        # миграции применятся при старте
docker compose logs -f backend
docker image prune -f
```

Лучшее время для обновления — середина недели, вдали от ребалансировки (понедельник 00:00 UTC).
Откат: `git checkout <предыдущий коммит> && docker compose up -d --build`; если новая версия
меняла схему БД — восстановить дамп, сделанный перед обновлением.

## 13. Мониторинг

| Что | Как |
|---|---|
| Сделки, ошибки, остановки | уведомления Telegram |
| Состояние | `/status` в Telegram, «Обзор» в панели, `docker compose ps` |
| Логи | `docker compose logs -f --since 1h backend` (JSON; `TB_LOG_JSON=false` — читаемый текст) |
| Внешняя проверка | Uptime Kuma / Healthchecks.io: раз в 5 минут `curl -fsS http://127.0.0.1:8000/api/health` из cron сервера с пингом сервиса — сообщит, если сервер целиком недоступен (Telegram-алерты в этом случае не придут) |
| Ресурсы | `docker stats`, `df -h` |

## 14. Контрольный список безопасности

- [ ] вход по SSH только по ключу, root запрещён, fail2ban включён;
- [ ] ufw: открыт только SSH (и 80/443 при HTTPS-панели);
- [ ] порты 5432 и 8000 привязаны к `127.0.0.1`;
- [ ] `.env` с правами 600, копия — в менеджере паролей;
- [ ] у пользователя панели включена 2FA;
- [ ] API-ключ Bybit: только спотовая торговля, без вывода и переводов, привязан к IP, на отдельном
      субаккаунте;
- [ ] бэкапы базы делаются и копируются за пределы сервера, восстановление проверено;
- [ ] время синхронизировано (`chronyc tracking`);
- [ ] автообновления безопасности включены.

## Типичные проблемы

| Симптом | Причина и решение |
|---|---|
| Сборка падает, `Killed` на `npm ci`/`npm run build` | не хватает памяти — добавьте swap (шаг 2) |
| Сборка падает на `npm ci`: `EIDLETIMEOUT`, `ETIMEDOUT`, `ECONNRESET` для `registry.npmjs.org` | нет стабильной связи с реестром npm. Проверьте с хоста: `curl -sI https://registry.npmjs.org/react`. **Хост отвечает, а сборка висит** — обычно MTU (VPN, ВМ): соберите в сети хоста `docker build --network=host -f backend/Dockerfile -t tradebot-backend .`, затем `docker compose up -d` (имя образа — `<папка>-backend`, см. вывод `docker compose build`). **Хост тоже не отвечает** — включите VPN на хосте или задайте зеркало в `.env`: `NPM_REGISTRY=https://registry.npmmirror.com/`, затем `docker compose build --no-cache backend && docker compose up -d` |
| Backend перезапускается, в логе `Fernet key must be 32 url-safe base64-encoded bytes` или `TB_MASTER_KEY не является ключом Fernet` | ключ в `.env` испорчен при копировании. Сгенерируйте и запишите его прямо на сервере: `KEY=$(docker run --rm --entrypoint python tradebot-backend -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")`, `sed -i "s#^TB_MASTER_KEY=.*#TB_MASTER_KEY=$KEY#" .env`, затем `docker compose up -d --force-recreate backend` |
| `invalid request, please check your server timestamp` | часы ушли — `chronyc tracking`, `sudo systemctl restart chrony` |
| Ошибки 403 / «region» от Bybit | страна сервера запрещена Bybit — переносите сервер |
| `API key is invalid` / `IP not in whitelist` | ключ от другой сети (testnet ↔ mainnet) или не тот IP в привязке |
| В paper с ключами движок не запускается, панель показывает ошибку | в paper разрешены только ключи testnet (`TB_BYBIT_TESTNET=true`) |
| `mode=live несовместим с bybit_testnet=true` | для live задайте `TB_BYBIT_TESTNET=false` |
| Backend не поднимается после смены `POSTGRES_PASSWORD` | пароль задаётся базе только при создании тома; верните старый или смените его в БД (`ALTER USER tradebot PASSWORD '...'`) |
| Нет уведомлений в Telegram | проверьте токен, `TB_TELEGRAM_CHAT_ID`, что вы написали боту первым |
