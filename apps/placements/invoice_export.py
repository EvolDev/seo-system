"""Выгрузка «Счетов» в виде листа «Счета» таблицы линкбилдинга (E1-14, ADR-055).

Лист «Счета» — строка на площадку: площадка, ссылка на статью, цена, цена за
пачку, ссылка на оплату, вебмастер или агентство, средняя цена за сайт. Здесь
так же: строка — размещение в счёте, «Цена» — его доля, «Цена за пачку» и
«Средняя цена за сайт» — у счёта больше одного размещения. Суммы — в валюте
счёта, она в колонке «Валюта». Счёт без размещений — одна строка без площадки.
Чего в листе нет — в конце: статус, даты, кто оплатил, продукт.

Строки — счета, отобранные в списке (фильтры, поиск, сортировка), все страницы.
"""

from collections.abc import Set
from typing import Any

from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from apps.placements.models import Invoice, InvoiceItem
from config.export import Choice, Column, Kind, Sheet, Value, narrow

SHEET = "Счета"
EXTRA = "Сверх листа таблицы"

_SPEC: tuple[tuple[str, Column, str], ...] = (
    ("site", Column("Площадка", width=28), SHEET),
    ("article_url", Column("Ссылка на статью", width=45), SHEET),
    ("share", Column("Цена", Kind.MONEY, 11), SHEET),
    ("total", Column("Цена за пачку", Kind.MONEY, 12), SHEET),
    ("pay_url", Column("Ссылка на оплату", width=40), SHEET),
    ("seller", Column("Вебмастер\\агентство", width=24), SHEET),
    ("average", Column("Средняя цена за сайт", Kind.MONEY, 12), SHEET),
    ("currency", Column("Валюта", width=7), EXTRA),
    ("status", Column("Статус", width=11), EXTRA),
    ("number", Column("Номер счёта", width=14), EXTRA),
    ("issued_on", Column("Выставлен", Kind.DATE, 12), EXTRA),
    ("paid_on", Column("Оплачен", Kind.DATE, 12), EXTRA),
    ("paid_by", Column("Оплатил", width=16), EXTRA),
    ("product", Column("Продукт", width=12), EXTRA),
    ("contacts", Column("Контакты продавца", width=30), EXTRA),
    ("comment", Column("Комментарий", width=30), EXTRA),
)


def choices() -> list[Choice]:
    return [Choice(key, column.title, group) for key, column, group in _SPEC]


def sheets(queryset: QuerySet[Invoice], wanted: Set[str]) -> list[Sheet]:
    items = InvoiceItem.objects.select_related("placement__site", "placement__product").order_by(
        "pk"
    )
    # Свои строки счёта — по порядку записи; prefetch списка сброшен (None).
    rows = (
        queryset.select_related("seller", "paid_by")
        .prefetch_related(None)
        .prefetch_related(Prefetch("items", queryset=items))
    )
    table: list[list[Value]] = []
    for invoice in rows:
        found: list[InvoiceItem | None] = list(invoice.items.all())
        batch = len(found) > 1
        for item in found or [None]:
            table.append(_row(invoice, item, batch, len(found)))
    layout = [(key, column) for key, column, _ in _SPEC]
    columns, values = narrow(layout, table, wanted)
    return [Sheet(SHEET, columns, list(values))]


def file_name() -> str:
    return f"Счета {timezone.localdate():%d.%m.%Y}"


def _row(invoice: Invoice, item: InvoiceItem | None, batch: bool, count: int) -> list[Value]:
    placement = item.placement if item is not None else None
    payer: Any = invoice.paid_by
    return [
        placement.site.domain if placement else None,
        placement.article_url if placement else None,
        item.amount_cents if item is not None else invoice.amount_cents,
        invoice.amount_cents if batch else None,
        invoice.pay_url,
        invoice.seller.name,
        round(invoice.amount_cents / count) if batch else None,
        invoice.currency,
        invoice.get_status_display(),
        invoice.number,
        invoice.issued_on,
        invoice.paid_on,
        (payer.get_full_name() or payer.get_username()) if payer is not None else None,
        placement.product.name if placement else None,
        invoice.seller.contacts,
        invoice.comment,
    ]
