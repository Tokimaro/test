import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.brokers.base import BrokerError
from app.brokers.bybit.client import BybitHttpClient

BASE = "https://bybit.test"


def ok(result: object, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"retCode": 0, "retMsg": "OK", "result": result}, headers=headers
    )


def err(code: int) -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": "fail", "result": {}})


def make_client(max_retries: int = 4) -> BybitHttpClient:
    return BybitHttpClient(
        base_url=BASE,
        api_key="KEY",
        api_secret="SECRET",
        backoff_base_s=0.0,
        max_retries=max_retries,
    )


@respx.mock
async def test_get_signature_covers_query_string() -> None:
    route = respx.get(f"{BASE}/v5/position/list").mock(return_value=ok({"list": []}))
    client = make_client()
    await client.request(
        "GET", "/v5/position/list", {"category": "linear", "symbol": None}, auth=True
    )
    req = route.calls.last.request
    assert req.url.query == b"category=linear"  # None-параметры отброшены
    ts = req.headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(
        b"SECRET", f"{ts}KEY5000category=linear".encode(), hashlib.sha256
    ).hexdigest()
    assert req.headers["X-BAPI-SIGN"] == expected
    assert req.headers["X-BAPI-API-KEY"] == "KEY"
    await client.aclose()


@respx.mock
async def test_post_signature_covers_exact_body() -> None:
    route = respx.post(f"{BASE}/v5/order/create").mock(return_value=ok({"orderId": "1"}))
    client = make_client()
    await client.request(
        "POST", "/v5/order/create", {"symbol": "BTCUSDT", "qty": "0.01"}, auth=True
    )
    req = route.calls.last.request
    body = req.content.decode()
    assert json.loads(body) == {"symbol": "BTCUSDT", "qty": "0.01"}
    ts = req.headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(b"SECRET", f"{ts}KEY5000{body}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["X-BAPI-SIGN"] == expected
    await client.aclose()


@respx.mock
async def test_public_request_has_no_auth_headers() -> None:
    route = respx.get(f"{BASE}/v5/market/kline").mock(return_value=ok({"list": []}))
    client = make_client()
    await client.request("GET", "/v5/market/kline", {"symbol": "BTCUSDT"})
    assert "X-BAPI-SIGN" not in route.calls.last.request.headers
    await client.aclose()


@respx.mock
async def test_retries_rate_limit_and_server_errors() -> None:
    route = respx.get(f"{BASE}/v5/market/tickers").mock(
        side_effect=[err(10006), httpx.Response(502), httpx.ConnectError("boom"), ok({"x": 1})]
    )
    client = make_client()
    assert await client.request("GET", "/v5/market/tickers") == {"x": 1}
    assert route.call_count == 4
    await client.aclose()


@respx.mock
async def test_gives_up_after_max_retries() -> None:
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=err(10006))
    client = make_client(max_retries=2)
    with pytest.raises(BrokerError) as exc:
        await client.request("GET", "/v5/market/tickers")
    assert exc.value.code == 10006
    await client.aclose()


@respx.mock
async def test_non_retryable_error_raises_immediately() -> None:
    route = respx.post(f"{BASE}/v5/order/create").mock(return_value=err(110007))
    client = make_client()
    with pytest.raises(BrokerError) as exc:
        await client.request("POST", "/v5/order/create", {}, auth=True)
    assert exc.value.code == 110007
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_timestamp_error_triggers_time_sync() -> None:
    respx.get(f"{BASE}/v5/market/time").mock(
        return_value=ok({"timeSecond": "1700000000", "timeNano": "1700000000000000000"})
    )
    route = respx.get(f"{BASE}/v5/account/wallet-balance").mock(
        side_effect=[err(10002), ok({"list": []})]
    )
    client = make_client()
    await client.request("GET", "/v5/account/wallet-balance", auth=True)
    # второй запрос подписан уже со смещённым временем сервера
    ts = int(route.calls.last.request.headers["X-BAPI-TIMESTAMP"])
    assert abs(ts - 1_700_000_000_000) < 60_000
    await client.aclose()


async def test_auth_without_keys_fails() -> None:
    client = BybitHttpClient(base_url=BASE)
    with pytest.raises(BrokerError):
        await client.request("GET", "/v5/position/list", auth=True)
    await client.aclose()


@respx.mock
async def test_http_4xx_is_not_retried() -> None:
    route = respx.get(f"{BASE}/v5/market/kline").mock(return_value=httpx.Response(403))
    client = make_client()
    with pytest.raises(BrokerError, match="HTTP 403"):
        await client.request("GET", "/v5/market/kline")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_non_json_response() -> None:
    respx.get(f"{BASE}/v5/market/kline").mock(return_value=httpx.Response(200, text="<html>"))
    client = make_client()
    with pytest.raises(BrokerError, match="не JSON"):
        await client.request("GET", "/v5/market/kline")
    await client.aclose()
