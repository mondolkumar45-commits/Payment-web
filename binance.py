"""Personal Binance API client — uses per-user credentials."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)
BASE_URL = "https://api.binance.com"


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


async def _request(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    params: dict | None = None,
) -> Any:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    query = urlencode(params)
    params["signature"] = _sign(api_secret, query)
    headers = {"X-MBX-APIKEY": api_key}

    url = BASE_URL + path
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params=params, headers=headers) if method == "GET" else \
               await client.request(method, url, params=params, headers=headers)
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Binance HTTP {resp.status_code}")
        if resp.status_code >= 400:
            msg = data.get("msg") or str(data)
            raise RuntimeError(f"Binance [{data.get('code', resp.status_code)}]: {msg}")
        return data


async def get_deposit_address(
    api_key: str, api_secret: str, coin: str, network: str
) -> dict:
    params: dict[str, Any] = {"coin": coin.upper()}
    if network:
        params["network"] = network.upper()
    return await _request(api_key, api_secret, "GET", "/sapi/v1/capital/deposit/address", params)


async def get_deposit_history(
    api_key: str,
    api_secret: str,
    *,
    coin: str | None = None,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    status: int | None = 1,
    limit: int = 200,
) -> list[dict]:
    params: dict[str, Any] = {"limit": min(limit, 1000)}
    if coin:
        params["coin"] = coin.upper()
    if start_time_ms:
        params["startTime"] = start_time_ms
    if end_time_ms:
        params["endTime"] = end_time_ms
    if status is not None:
        params["status"] = status
    data = await _request(api_key, api_secret, "GET", "/sapi/v1/capital/deposit/hisrec", params)
    if isinstance(data, list):
        return data
    return data.get("depositList") or []
