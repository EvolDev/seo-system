"""Курсы валют ЕЦБ: сколько единиц валюты за 1 евро (ADR-043).

Официальный API ЕЦБ (SDMX), без ключа, ответ — CSV. Берём несколько
последних наблюдений по каждой валюте: пропущенный день (воркер стоял)
догоняется при следующем запуске. ЕЦБ публикует курсы в рабочие дни
около 16:00 по центральноевропейскому времени; в выходные и праздники
новых нет.
"""

import csv
import datetime as dt
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

URL = "https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A"
TIMEOUT_SECONDS = 30
# Наблюдений на валюту: неделя рабочих дней — догнать пропуски после простоя.
LAST_OBSERVATIONS = 5


class EcbError(Exception):
    """ЕЦБ недоступен или ответил не тем — задача очереди повторит попытку."""


@dataclass(frozen=True)
class Rate:
    currency: str
    rate_date: dt.date
    rate: Decimal


def fetch_rates(transport: httpx.BaseTransport | None = None) -> list[Rate]:
    """Последние курсы всех валют ЕЦБ к евро.

    `transport` подменяют тесты: запрос уходит в функцию, а не в сеть.
    """
    params: dict[str, str | int] = {"lastNObservations": LAST_OBSERVATIONS, "format": "csvdata"}
    # `with` закрывает соединение по выходу из блока, даже при ошибке.
    with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        try:
            response = client.get(URL, params=params)
        except httpx.HTTPError as error:
            raise EcbError(f"ЕЦБ недоступен: {type(error).__name__}: {error}") from error
    if response.status_code != httpx.codes.OK:
        raise EcbError(f"ЕЦБ ответил {response.status_code}: {response.text[:200]}")
    rates = parse_csv(response.text)
    if not rates:
        raise EcbError("ЕЦБ не прислал ни одного курса")
    return rates


def parse_csv(text: str) -> list[Rate]:
    """Курсы из CSV ЕЦБ: колонки CURRENCY, TIME_PERIOD, OBS_VALUE.

    Строка без значения (ЕЦБ так отмечает пропуск) не попадает в результат.
    """
    rates = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            currency = row["CURRENCY"].strip().upper()
            rate_date = dt.date.fromisoformat(row["TIME_PERIOD"].strip())
            value = row["OBS_VALUE"].strip()
        except (KeyError, AttributeError, ValueError) as error:
            raise EcbError(f"ЕЦБ прислал CSV не того формата: {error}") from error
        if not value:
            continue
        try:
            rate = Decimal(value)
        except InvalidOperation as error:
            raise EcbError(f"Курс {currency} на {rate_date} — не число: {value!r}") from error
        if len(currency) == 3 and rate > 0:
            rates.append(Rate(currency, rate_date, rate))
    return rates
