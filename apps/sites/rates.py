"""Курсы валют к евро: запись снимков ЕЦБ (ADR-043).

Пересчёт в евро делают представления (функция `eur_rate` в `schema.sql`):
последний известный курс, на сегодня его может не быть — выходные,
праздники, курс ещё не опубликован.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from apps.integrations.ecb import Rate
from apps.sites.models import ExchangeRate


def save_rates(rates: Iterable[Rate]) -> int:
    """Записывает курсы, которых ещё нет. Возвращает, сколько новых.

    Курс на дату ЕЦБ не меняет — уже записанный не трогаем.
    """
    rows = [ExchangeRate(currency=r.currency, rate_date=r.rate_date, rate=r.rate) for r in rates]
    # ignore_conflicts — это ON CONFLICT DO NOTHING: пара «валюта, дата» уже есть — пропуск.
    # Сколько вставлено, bulk_create в этом режиме не скажет — считаем до и после.
    before = ExchangeRate.objects.count()
    ExchangeRate.objects.bulk_create(rows, ignore_conflicts=True)
    return ExchangeRate.objects.count() - before


def latest_rates() -> dict[str, Decimal]:
    """Последний известный курс каждой валюты за 1 евро, как `eur_rate()` в SQL."""
    rates: dict[str, Decimal] = {"EUR": Decimal(1)}
    # DISTINCT ON (currency) с сортировкой по дате — последняя строка каждой валюты.
    latest = ExchangeRate.objects.order_by("currency", "-rate_date").distinct("currency")
    for row in latest:
        rates.setdefault(row.currency, row.rate)
    return rates


def to_eur_cents(cents: int, currency: str, rates: dict[str, Decimal]) -> int | None:
    """Центы в валюте → евроценты по курсу; курса нет — None.

    Округление как у `round()` в Postgres для numeric: половина — от нуля
    (Decimal по умолчанию округлял бы к чётному).
    """
    rate = rates.get(currency)
    if rate is None:
        return None
    return int((Decimal(cents) / rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
