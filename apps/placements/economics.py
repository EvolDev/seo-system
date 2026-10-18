"""Экономика размещений: бюджет месяца, потрачено, запланировано, остаток.

Правила счёта (пользователь, 14.10.2026):
- потрачено — размещения «Размещено»: деньги уже перечислены. Сумма —
  «Заплачено» в евро по курсу ЕЦБ, месяц — по дате «Опубликовано»;
- запланировано — «В работе», «Заявка отправлена», «Написание статьи».
  Сумма — «Заплачено», если там уже не ноль (договорённая, из счёта), иначе
  сколько заплатим площадке по рабочей цене (`expected_spend_cents` из
  `v_product_site_latest` — одна точка правды, ADR-043). Месяц — по дате
  отправки заявки, без неё — текущий: «собираемся потратить сейчас»;
- бюджет — настройка `MONTHLY_BUDGET` на каждый месяц, остаток не переносится;
- копирайт — цена статьи нашему копирайтеру (ADR-072): в тот же месяц, что и
  размещение, отдельно от бюджета — платим за него не мы.

Отчёт — сырым SQL (ADR-003): одна выборка размещений, дальше — суммы в Python
(размещений сотни, не тысячи).
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.utils import timezone

from apps.content.domain_settings import monthly_budget
from apps.placements.models import PlacementStatus

PLANNED = (PlacementStatus.IN_WORK, PlacementStatus.ORDERED, PlacementStatus.WRITING)
SPENT = PlacementStatus.PLACED

# Ожидаемая сумма плана — из представления, но только у нужных площадок: условие
# «site_id = ANY(массив)» план Postgres доводит до индекса, соединение с
# представлением — нет (было 2 с против 0,2 с на рабочей базе).
_ROWS_SQL = """
WITH rows AS MATERIALIZED (
    SELECT p.id, p.site_id, p.product_id, p.status::text AS status, p.seller_id,
           p.employee_id, p.ordered_at, p.published_at, p.price_paid_cents,
           coalesce(p.currency, 'EUR') AS currency, p.copywriter_id, p.copywriter_cents,
           coalesce(p.copywriter_currency, 'EUR') AS copywriter_currency
    FROM placements p
    WHERE p.status IN ('in_work', 'ordered', 'writing', 'placed')
      AND (%(product)s::bigint IS NULL OR p.product_id = %(product)s::bigint)
),
expected AS MATERIALIZED (
    SELECT v.site_id, v.product_id, v.expected_spend_cents
    FROM v_product_site_latest v
    WHERE v.site_id = ANY(ARRAY(SELECT r.site_id FROM rows r WHERE r.status <> 'placed'))
      AND v.product_id = ANY(ARRAY(SELECT r.product_id FROM rows r WHERE r.status <> 'placed'))
)
SELECT r.id, r.site_id, s.domain, pr.name, r.status, se.name,
       coalesce(nullif(u.first_name, ''), u.username),
       r.ordered_at, r.published_at,
       CASE WHEN r.status = 'placed'
            THEN date_trunc('month', r.published_at AT TIME ZONE %(tz)s)::date
            ELSE coalesce(date_trunc('month', r.ordered_at AT TIME ZONE %(tz)s)::date,
                          %(current)s::date)
       END AS month,
       CASE WHEN r.status = 'placed' OR r.price_paid_cents > 0
            THEN round(r.price_paid_cents / eur_rate(r.currency))::bigint
            ELSE e.expected_spend_cents
       END AS eur_cents,
       r.status = 'placed' OR r.price_paid_cents > 0 AS from_paid,
       r.price_paid_cents, r.currency,
       cw.name, round(r.copywriter_cents / eur_rate(r.copywriter_currency))::bigint,
       r.copywriter_cents IS NOT NULL AND r.copywriter_currency <> 'EUR'
FROM rows r
JOIN sites s ON s.id = r.site_id
JOIN products pr ON pr.id = r.product_id
LEFT JOIN sellers se ON se.id = r.seller_id
LEFT JOIN auth_user u ON u.id = r.employee_id
LEFT JOIN copywriters cw ON cw.id = r.copywriter_id
LEFT JOIN expected e ON e.site_id = r.site_id AND e.product_id = r.product_id
ORDER BY r.id
"""


@dataclass(frozen=True)
class Row:
    """Размещение в экономике: потрачено оно или в плане, месяц и сумма в евро."""

    id: int
    site_id: int
    domain: str
    product: str
    status: str
    seller: str
    employee: str
    ordered_at: dt.datetime | None
    published_at: dt.datetime | None
    month: dt.date | None  # первое число; у размещённого без даты публикации — нет
    eur_cents: int | None  # нет — сумму не посчитать: нет «Заплачено» или рабочей цены
    from_paid: bool  # сумма из «Заплачено», иначе — по рабочей цене площадки
    paid_cents: int | None
    currency: str
    copywriter: str = ""  # пусто — копирайтер не назначен
    copy_cents: int | None = None  # цена статьи копирайтеру, евро-центы
    copy_converted: bool = False

    @property
    def spent(self) -> bool:
        return self.status == SPENT

    @property
    def converted(self) -> bool:
        """Сумма не в евро — пересчитана по курсу ЕЦБ, в отчёте с «≈»."""
        return self.from_paid and self.currency != "EUR"


@dataclass
class Month:
    month: dt.date
    budget_cents: int = 0
    spent_cents: int = 0
    planned_cents: int = 0
    placed: int = 0  # размещено ссылок
    priced: int = 0  # из них с суммой — для средней цены
    planned: int = 0
    converted: bool = False
    by_employee: dict[str, int] = field(default_factory=dict)
    copy_cents: int = 0  # копирайт размещённых — не из бюджета
    copy_planned_cents: int = 0  # копирайт статей в работе
    copy_converted: bool = False

    @property
    def total_cents(self) -> int:
        return self.spent_cents + self.planned_cents

    @property
    def left_cents(self) -> int:
        return self.budget_cents - self.spent_cents - self.planned_cents

    @property
    def average_cents(self) -> int | None:
        return round(self.spent_cents / self.priced) if self.priced else None

    @property
    def with_copy_cents(self) -> int:
        """Потрачено + копирайт: во что обошлось размещение вместе со статьёй."""
        return self.spent_cents + self.copy_cents

    @property
    def average_with_copy_cents(self) -> int | None:
        return round(self.with_copy_cents / self.priced) if self.priced else None

    def add(self, row: Row) -> None:
        cents = row.eur_cents or 0
        if row.spent:
            self.placed += 1
            self.spent_cents += cents
            self.priced += row.eur_cents is not None
            name = row.employee or ""
            self.by_employee[name] = self.by_employee.get(name, 0) + 1
        else:
            self.planned += 1
            self.planned_cents += cents
        self.converted = self.converted or row.converted
        if row.copy_cents is not None:
            if row.spent:
                self.copy_cents += row.copy_cents
            else:
                self.copy_planned_cents += row.copy_cents
            self.copy_converted = self.copy_converted or row.copy_converted


@dataclass(frozen=True)
class Period:
    """Месяцы отчёта: с первого числа `start` до первого числа `end` (не включая)."""

    start: dt.date
    end: dt.date

    @property
    def months(self) -> list[dt.date]:
        return months_between(self.start, self.end)

    def __contains__(self, month: object) -> bool:
        return isinstance(month, dt.date) and self.start <= month < self.end


@dataclass(frozen=True)
class Group:
    """Строка разбивки: статус плана или продавец."""

    name: str
    count: int
    cents: int
    spent_cents: int = 0
    planned_cents: int = 0


@dataclass
class Report:
    period: Period
    budget_month_cents: int  # бюджет одного месяца
    budgets: list[tuple[str, int]]  # у «Всех продуктов» — по продуктам
    months: list[Month]  # месяцы графиков: весь срок работы, не только период
    total: Month  # итог периода
    rows: list[Row]  # размещения периода
    by_status: list[Group]
    by_seller: list[Group]
    by_copywriter: list[Group]  # spent — копирайт размещённых, planned — в работе
    employees: list[str]  # колонки «размещено» по сотрудникам, в порядке убывания
    no_price: int  # в плане без суммы: нет рабочей цены или порога написания
    no_order_date: int  # в плане без даты отправки — посчитаны текущим месяцем
    no_paid: int  # размещено без «Заплачено»
    no_published: int  # размещено без даты публикации — ни в один месяц не попали
    no_copy_price: int  # копирайтер назначен, цены статьи нет
    current: dt.date  # текущий месяц


def months_between(start: dt.date, end: dt.date) -> list[dt.date]:
    result = []
    month = start
    while month < end:
        result.append(month)
        month = add_months(month, 1)
    return result


def add_months(month: dt.date, count: int) -> dt.date:
    index = month.year * 12 + month.month - 1 + count
    return dt.date(index // 12, index % 12 + 1, 1)


def rows(product_id: int | None, today: dt.date | None = None) -> list[Row]:
    """Все размещения в работе и размещённые — с месяцем и суммой в евро."""
    current = (today or timezone.localdate()).replace(day=1)
    params: dict[str, Any] = {
        "product": product_id,
        "tz": timezone.get_current_timezone_name(),
        "current": current,
    }
    with connection.cursor() as cursor:
        cursor.execute(_ROWS_SQL, params)
        fetched = cursor.fetchall()
    return [
        Row(
            id=int(pk),
            site_id=int(site_id),
            domain=str(domain),
            product=str(product),
            status=str(status),
            seller=str(seller or ""),
            employee=str(employee or ""),
            ordered_at=ordered_at,
            published_at=published_at,
            month=month,
            eur_cents=None if cents is None else int(cents),
            from_paid=bool(from_paid),
            paid_cents=None if paid is None else int(paid),
            currency=str(currency),
            copywriter=str(copywriter or ""),
            copy_cents=None if copy is None else int(copy),
            copy_converted=bool(copy_converted),
        )
        for (
            pk,
            site_id,
            domain,
            product,
            status,
            seller,
            employee,
            ordered_at,
            published_at,
            month,
            cents,
            from_paid,
            paid,
            currency,
            copywriter,
            copy,
            copy_converted,
        ) in fetched
    ]


def budgets(products: Iterable[tuple[int, str]]) -> list[tuple[str, int]]:
    """Бюджет месяца по продуктам, евро-центы: своё значение продукта или общее."""
    return [(name, monthly_budget(pk).eur_cents) for pk, name in products]


# Графики показывают не больше двух лет: дальше столбики слишком тонкие.
CHART_MONTHS = 24


def report(
    period: Period,
    products: list[tuple[int, str]],
    product_id: int | None,
    today: dt.date | None = None,
) -> Report:
    """Отчёт за период. `products` — продукты отчёта: один или все активные."""
    today = today or timezone.localdate()
    current = today.replace(day=1)
    found = rows(product_id, today)
    shares = budgets(products)
    budget = sum(cents for _name, cents in shares)

    # Графики — от первого месяца с размещениями до текущего (и до конца периода),
    # чтобы период было видно на фоне всей работы, как в таблице «Линкбилдинг».
    dated = [row.month for row in found if row.month is not None]
    first = min([*dated, period.start, current])
    last = add_months(max([*dated, add_months(period.end, -1), current]), 1)
    first = max(first, add_months(last, -CHART_MONTHS))
    months = {month: Month(month, budget_cents=budget) for month in months_between(first, last)}
    for row in found:
        if row.month in months:
            months[row.month].add(row)

    chosen = [row for row in found if row.month in period]
    total = Month(period.start, budget_cents=budget * len(period.months))
    for row in chosen:
        total.add(row)

    planned = [row for row in chosen if not row.spent]
    by_status = [
        Group(
            str(status.label),
            sum(1 for row in planned if row.status == status),
            sum(row.eur_cents or 0 for row in planned if row.status == status),
        )
        for status in PLANNED
    ]
    sellers: dict[str, list[Row]] = {}
    for row in chosen:
        sellers.setdefault(row.seller or "без продавца", []).append(row)
    by_seller = sorted(
        (
            Group(
                name,
                len(items),
                sum(row.eur_cents or 0 for row in items),
                spent_cents=sum(row.eur_cents or 0 for row in items if row.spent),
                planned_cents=sum(row.eur_cents or 0 for row in items if not row.spent),
            )
            for name, items in sellers.items()
        ),
        key=lambda group: (-group.cents, group.name),
    )
    writers: dict[str, list[Row]] = {}
    for row in chosen:
        if row.copywriter:
            writers.setdefault(row.copywriter, []).append(row)
    by_copywriter = sorted(
        (
            Group(
                name,
                len(items),
                sum(row.copy_cents or 0 for row in items),
                spent_cents=sum(row.copy_cents or 0 for row in items if row.spent),
                planned_cents=sum(row.copy_cents or 0 for row in items if not row.spent),
            )
            for name, items in writers.items()
        ),
        key=lambda group: (-group.cents, group.name),
    )
    employees: dict[str, int] = {}
    for month in months.values():
        for name, count in month.by_employee.items():
            employees[name] = employees.get(name, 0) + count
    return Report(
        period=period,
        budget_month_cents=budget,
        budgets=shares,
        months=list(months.values()),
        total=total,
        rows=sorted(chosen, key=lambda row: (-(row.eur_cents or 0), row.domain)),
        by_status=by_status,
        by_seller=by_seller,
        by_copywriter=by_copywriter,
        employees=sorted(employees, key=lambda name: (-employees[name], name == "", name)),
        no_price=sum(1 for row in planned if row.eur_cents is None),
        no_order_date=sum(1 for row in planned if row.ordered_at is None),
        no_paid=sum(1 for row in chosen if row.spent and row.eur_cents is None),
        no_published=sum(1 for row in found if row.spent and row.published_at is None),
        no_copy_price=sum(1 for row in chosen if row.copywriter and row.copy_cents is None),
        current=current,
    )
