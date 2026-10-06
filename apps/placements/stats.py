"""Статистика размещений для главной: по месяцам года и этот месяц (E1-14, ADR-055).

Размещено — опубликованные размещения по месяцу публикации (время проекта).
Потрачено — их «Заплачено» в евро по курсу ЕЦБ, той же суммой, что отчёт за
месяц (ADR-053): месяц трат — месяц публикации. Делится на «по счетам» (у
размещения неотменённый счёт продавца) и «без счёта» (Collaborator и прочее).
Валюта не евро — пересчёт по последнему курсу, сумма помечается «≈».

Отчёт — сырым SQL (ADR-003): одна выборка на год и одна на этот и прошлый месяц.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.db import connection
from django.utils import timezone

# Месяц публикации, сумма в евро и «по счёту ли» — по одному размещению; дальше
# группировка по месяцам. eur_rate — курс за 1 евро, NULL — курса нет.
_MONTHS_SQL = """
WITH placed AS (
    SELECT date_trunc('month', p.published_at AT TIME ZONE %(tz)s)::date AS month,
           p.price_paid_cents / eur_rate(coalesce(p.currency, 'EUR')) AS paid_eur,
           p.price_paid_cents IS NOT NULL AND coalesce(p.currency, 'EUR') <> 'EUR' AS converted,
           EXISTS (
               SELECT 1 FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id
               WHERE ii.placement_id = p.id AND i.status <> 'cancelled'
           ) AS invoiced
    FROM placements p
    WHERE p.status = 'placed'
      AND p.published_at >= %(start)s AND p.published_at < %(end)s
      AND (%(product)s::bigint IS NULL OR p.product_id = %(product)s::bigint)
)
SELECT month,
       count(*),
       coalesce(round(sum(paid_eur) FILTER (WHERE invoiced)), 0)::bigint,
       coalesce(round(sum(paid_eur) FILTER (WHERE NOT invoiced)), 0)::bigint,
       bool_or(converted)
FROM placed
GROUP BY month
ORDER BY month
"""

_NO_DATE_SQL = """
SELECT count(*) FROM placements p
WHERE p.status = 'placed' AND p.published_at IS NULL
  AND (%(product)s::bigint IS NULL OR p.product_id = %(product)s::bigint)
"""


@dataclass(frozen=True)
class Month:
    month: dt.date  # первое число
    placed: int
    invoiced_cents: int  # потрачено по счетам, евро-центы
    other_cents: int  # без счёта: Collaborator и прочее
    converted: bool  # были суммы не в евро — пересчёт по курсу

    @property
    def spent_cents(self) -> int:
        return self.invoiced_cents + self.other_cents


def months(year: int, product_id: int | None) -> list[Month]:
    """Двенадцать месяцев года, пустые — нулями."""
    start = dt.date(year, 1, 1)
    found = _query(start, dt.date(year + 1, 1, 1), product_id)
    return [found.get(dt.date(year, number, 1)) or _empty(year, number) for number in range(1, 13)]


def this_and_previous(today: dt.date, product_id: int | None) -> tuple[Month, Month]:
    """Этот месяц и прошлый — для плиток «Этот месяц», хоть год на графике и другой."""
    current = today.replace(day=1)
    previous = (current - dt.timedelta(days=1)).replace(day=1)
    following = (current + dt.timedelta(days=32)).replace(day=1)
    found = _query(previous, following, product_id)
    return (
        found.get(current) or _empty(current.year, current.month),
        found.get(previous) or _empty(previous.year, previous.month),
    )


def without_date(product_id: int | None) -> int:
    """Опубликованные без даты публикации: в месяцы они не попадают."""
    with connection.cursor() as cursor:
        cursor.execute(_NO_DATE_SQL, {"product": product_id})
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def _query(start: dt.date, end: dt.date, product_id: int | None) -> dict[dt.date, Month]:
    params: dict[str, Any] = {
        "tz": timezone.get_current_timezone_name(),
        "start": _midnight(start),
        "end": _midnight(end),
        "product": product_id,
    }
    with connection.cursor() as cursor:
        cursor.execute(_MONTHS_SQL, params)
        rows = cursor.fetchall()
    return {
        month: Month(month, int(placed), int(invoiced), int(other), bool(converted))
        for month, placed, invoiced, other, converted in rows
    }


def _midnight(day: dt.date) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


def _empty(year: int, month: int) -> Month:
    return Month(dt.date(year, month, 1), 0, 0, 0, False)


def nice_ticks(top: int, count: int = 4) -> list[int]:
    """Деления оси: 0 и круглые числа до верха не меньше `top` (1, 2, 5 × 10ⁿ)."""
    if top <= 0:
        return [0, 1]
    raw = top / count
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    ticks = [0]
    while ticks[-1] < top:
        ticks.append(ticks[-1] + step)
    return ticks
