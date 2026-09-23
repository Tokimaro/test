"""Асинхронный REST-клиент Bybit V5: подпись запросов, синхронизация времени,
соблюдение rate limit и повторы при временных ошибках.

Документация: https://bybit-exchange.github.io/docs/v5/intro
"""

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from app.brokers.base import BrokerError

log = structlog.get_logger()

MAINNET_REST = "https://api.bybit.com"
TESTNET_REST = "https://api-testnet.bybit.com"

# retCode, при которых запрос безопасно повторить
RETRYABLE_CODES = {
    10000,  # server timeout
    10006,  # too many visits (rate limit)
    10016,  # server error
    10019,  # service restarting
}
TIMESTAMP_ERROR = 10002  # timestamp вне recv_window — нужна пересинхронизация времени


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    """Token bucket: не более `rate` запросов за `per` секунд."""

    def __init__(
        self, rate: int, per: float = 1.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._capacity = float(rate)
        self._tokens = float(rate)
        self._fill_rate = rate / per
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def block_until(self, monotonic_ts: float) -> None:
        """Биржа сообщила, что лимит исчерпан до указанного момента."""
        self._blocked_until = max(self._blocked_until, monotonic_ts)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await asyncio.sleep(self._blocked_until - now)
                    continue
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._fill_rate
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._fill_rate)


class BybitHttpClient:
    def __init__(
        self,
        *,
        testnet: bool = True,
        api_key: str = "",
        api_secret: str = "",
        recv_window_ms: int = 5000,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        rate_per_second: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = str(recv_window_ms)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._time_offset_ms = 0
        self._limiter = RateLimiter(rate_per_second)
        self._http = httpx.AsyncClient(
            base_url=base_url or (TESTNET_REST if testnet else MAINNET_REST),
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    async def sync_time(self) -> int:
        """Считает смещение локальных часов относительно сервера. Возвращает время сервера."""
        t0 = int(time.time() * 1000)
        result = await self.request("GET", "/v5/market/time")
        t1 = int(time.time() * 1000)
        server_ms = int(result["timeNano"]) // 1_000_000
        self._time_offset_ms = server_ms - (t0 + t1) // 2
        log.debug("bybit.time_synced", offset_ms=self._time_offset_ms)
        return server_ms

    def _auth_headers(self, payload: str) -> dict[str, str]:
        ts = str(self._now_ms())
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": sign(self._api_secret, ts + self._api_key + self._recv_window + payload),
        }

    def _apply_rate_headers(self, headers: httpx.Headers) -> None:
        remaining = headers.get("X-Bapi-Limit-Status")
        reset_ms = headers.get("X-Bapi-Limit-Reset-Timestamp")
        if remaining is not None and reset_ms is not None and int(remaining) <= 0:
            wait_s = max(0.0, (int(reset_ms) - self._now_ms()) / 1000)
            self._limiter.block_until(time.monotonic() + wait_s)

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        auth: bool = False,
    ) -> dict[str, Any]:
        """Выполняет запрос и возвращает поле `result`. Бросает BrokerError при retCode != 0."""
        if auth and not self.has_credentials:
            raise BrokerError("для приватного запроса нужны API-ключи")
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire()
            try:
                resp = await self._send(method, path, clean, auth)
            except (httpx.TransportError, _HttpServerError) as exc:
                if attempt > self._max_retries:
                    raise BrokerError(f"{method} {path}: сеть недоступна: {exc}") from exc
                await self._backoff(attempt, path, str(exc))
                continue

            self._apply_rate_headers(resp.headers)
            try:
                body = resp.json()
            except ValueError as exc:
                raise BrokerError(f"{method} {path}: ответ не JSON: {resp.text[:200]}") from exc
            code = int(body.get("retCode", -1))
            if code == 0:
                result: dict[str, Any] = body.get("result") or {}
                return result
            msg = f"{method} {path}: retCode={code} {body.get('retMsg')}"
            can_retry = attempt <= self._max_retries
            if can_retry and code == TIMESTAMP_ERROR and path != "/v5/market/time":
                await self.sync_time()
                continue
            if can_retry and code in RETRYABLE_CODES:
                await self._backoff(attempt, path, msg)
                continue
            raise BrokerError(msg, code=code)

    async def _send(
        self, method: str, path: str, params: dict[str, Any], auth: bool
    ) -> httpx.Response:
        if method == "GET":
            query = urlencode(params)
            headers = self._auth_headers(query) if auth else {}
            resp = await self._http.get(path + (f"?{query}" if query else ""), headers=headers)
        else:
            body = json.dumps(params, separators=(",", ":"))
            headers = {"Content-Type": "application/json"}
            if auth:
                headers.update(self._auth_headers(body))
            resp = await self._http.request(method, path, content=body, headers=headers)
        if resp.status_code >= 500 or resp.status_code == 429:
            raise _HttpServerError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BrokerError(f"{method} {path}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _backoff(self, attempt: int, path: str, reason: str) -> None:
        delay = self._backoff_base * 2 ** (attempt - 1)
        log.warning("bybit.retry", path=path, attempt=attempt, delay_s=delay, reason=reason)
        await asyncio.sleep(delay)


class _HttpServerError(Exception):
    pass
