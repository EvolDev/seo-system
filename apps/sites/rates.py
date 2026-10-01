"""Курсы валют к евро: запись снимков ЕЦБ (ADR-043).

Пересчёт в евро делают представления (функция `eur_rate` в `schema.sql`):
последний известный курс, на сегодня его может не быть — выходные,
праздники, курс ещё не опубликован.
"""

from collections.abc import Iterable

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
