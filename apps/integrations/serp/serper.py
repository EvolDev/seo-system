"""Провайдер выдачи Serper (serper.dev), ADR-040.

Отвечает сразу, очереди подешевле у него нет. Платим кредитами: запрос
до 10 результатов — один кредит, глубже — больше; сколько списано, Serper
пишет в ответе (`credits`). Цена кредита зависит от купленного пакета —
она в настройках (`SERPER_CENTS_PER_1000`), а не в коде.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.utils import timezone

from config.env import env_str

from .types import ProviderAnswer, SerpError, SerpPage, SerpResult

URL = "https://google.serper.dev/search"
ENDPOINT = "search"
TIMEOUT_SECONDS = 30


class SerperProvider:
    name = "serper"

    def __init__(
        self,
        api_key: str,
        cents_per_1000: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._cents_per_1000 = cents_per_1000
        # transport подменяют тесты: запросы уходят в функцию, а не в сеть.
        self._transport = transport

    @classmethod
    def from_settings(cls) -> "SerperProvider":
        return cls(env_str("SERPER_API_KEY"), settings.SERPER_CENTS_PER_1000)

    def fetch(self, query: str, depth: int, country: str) -> ProviderAnswer:
        body = self._post({"q": query, "gl": country, "num": depth})
        credits = body.get("credits")
        if not isinstance(credits, int):
            # Без списанных кредитов не посчитать расход — лучше упасть, чем
            # тратить деньги мимо учёта и лимита.
            raise SerpError(f"Serper не сообщил, сколько кредитов списано: {credits!r}")
        page = SerpPage(
            query=query,
            depth=depth,
            country=country,
            results=tuple(_result(item) for item in body.get("organic") or []),
            total_estimate=_total_estimate(body),
            fetched_at=_now(),
        )
        cost = Decimal(credits * self._cents_per_1000) / 1000
        return ProviderAnswer(page=page, endpoint=ENDPOINT, units=credits, cost_cents=cost)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # `with` закрывает соединение по выходу из блока, даже при ошибке.
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=self._transport) as client:
            try:
                response = client.post(URL, json=payload, headers={"X-API-KEY": self._api_key})
            except httpx.HTTPError as error:
                raise SerpError(f"Serper недоступен: {type(error).__name__}: {error}") from error
        if response.status_code != httpx.codes.OK:
            raise SerpError(f"Serper ответил {response.status_code}: {_message(response)}")
        try:
            body = response.json()
        except ValueError as error:
            raise SerpError("Serper ответил не JSON") from error
        if not isinstance(body, dict):
            raise SerpError("Serper ответил не объектом JSON")
        return body


def _result(item: dict[str, Any]) -> SerpResult:
    url = str(item.get("link") or "")
    return SerpResult(
        position=int(item.get("position") or 0),
        url=url,
        domain=(urlsplit(url).hostname or "").lower(),
        title=str(item.get("title") or ""),
        snippet=str(item.get("snippet") or ""),
    )


def _total_estimate(body: dict[str, Any]) -> int | None:
    """«Примерно N результатов», если Serper его прислал."""
    info = body.get("searchInformation")
    if isinstance(info, dict):
        value = info.get("totalResults")
        if isinstance(value, int | str) and str(value).isdigit():
            return int(value)
    return None


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return response.text[:200]


def _now() -> datetime:
    return timezone.now()
