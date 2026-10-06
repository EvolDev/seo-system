"""Выгрузка «Размещений» — отчёт за месяц в виде листа «Размещения» (E1-06, ADR-053).

Лист «Размещения» таблицы линкбилдинга — то, что пользователь отдаёт
начальству: те же колонки, в том же порядке, с теми же названиями (Target,
Источник, URL статьи, …, Итог цена). «Итог цена» — сколько заплатили
(`price_paid_cents`), её вписывают при закрытии заявки. Колонки площадки
(трафик, цены каталога, тематики) — данные площадки на день выгрузки.
Системное, чего в листе нет, — в конце: продукт, сотрудник, заявка, итог в
евро. Второй лист «Итого» — число размещений и сумма в евро: всего, по
продавцам и по услугам.

Строки — те, что отобраны в списке «Размещений» (фильтры, поиск,
сортировка), все страницы.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence, Set
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.db.models import F, QuerySet

from apps.placements import invoices
from apps.placements.models import (
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    Placement,
    PlacementLink,
    PlacementStatus,
)
from apps.sites.models import PlacementType, Product
from apps.sites.rates import latest_rates, to_eur_cents
from config.export import Choice, Column, Kind, Sheet, Value, narrow

STATUS = dict(PlacementStatus.choices)
SERVICE = dict(PlacementType.choices)
NOT_SET = "не указан"
# Пар «Анкор / Ссылка» в листе две; у размещения ссылок больше — колонок больше.
SHEET_LINKS = 2

PLACEMENT = "Размещение"
SITE = "Площадка на сегодня"
EXTRA = "Сверх листа таблицы"
INVOICE = "Счёт"
# Галочки на группу колонок: все пары «Анкор / Ссылка», все «Пример статьи на …».
LINKS = "links"
EXAMPLES = "examples"
_GROUP_TITLES = {LINKS: "Анкоры и ссылки", EXAMPLES: "Пример статьи на другом продукте"}

# Колонки листа «Размещения» по порядку: ключ галочки, колонка (у групп — None,
# их колонки строит `_layout`), раздел окна выгрузки. Порядок значений строки —
# тот же (`_row`).
_SPEC: tuple[tuple[str, Column | None, str], ...] = (
    ("target", Column("Target", width=28), PLACEMENT),
    ("seller", Column("Источник", width=18), PLACEMENT),
    ("site_note", Column("Комментарий к площадке", width=30), SITE),
    ("article_url", Column("URL статьи", width=45), PLACEMENT),
    ("indexed", Column("Индексация", width=10), PLACEMENT),
    ("status", Column("Статус", width=16), PLACEMENT),
    ("published", Column("Дата размещения", Kind.DATE, 12), PLACEMENT),
    ("month", Column("Месяц", Kind.MONTH, 14), PLACEMENT),
    ("comment", Column("Комментарий по размещению", width=30), PLACEMENT),
    (LINKS, None, PLACEMENT),
    (EXAMPLES, None, SITE),
    ("traffic", Column("Organic / Traffic", Kind.INT, 12), SITE),
    ("top_geo", Column("Top Geo", width=7), SITE),
    ("top_geo_traffic", Column("Top Geo Traff", Kind.INT, 12), SITE),
    ("us_traffic", Column("US Traff", Kind.INT, 12), SITE),
    ("dr", Column("DR", Kind.INT, 6), SITE),
    ("keywords", Column("Organic / Total Keywords", Kind.INT, 12), SITE),
    ("service", Column("Тип ссылки", width=14), PLACEMENT),
    ("placement_price", Column("Цена размещения статья, EUR", Kind.MONEY, 12), SITE),
    ("announce_price", Column("Цена анонса статья, EUR", Kind.MONEY, 12), SITE),
    ("writing_price", Column("Цена написания статья, EUR", Kind.MONEY, 12), SITE),
    ("paid", Column("Итог цена", Kind.MONEY, 11), PLACEMENT),
    ("links_allowed", Column("Количество ссылок статья", Kind.INT, 9), SITE),
    ("marks_as_ad", Column("Пометка о рекламе статья", width=9), SITE),
    ("declared_topics", Column("Особые тематики", width=24), SITE),
    ("languages", Column("Языки сайта", width=16), SITE),
    ("site_type", Column("Тип сайта", width=18), SITE),
    ("collaborator_url", Column("URL Коллаборатора", width=30), SITE),
    ("topics", Column("Тематика", width=30), SITE),
    ("link_type", Column("Тип ссылки статья", width=10), SITE),
    # Чего в листе нет — в конце.
    ("product", Column("Продукт", width=12), EXTRA),
    ("employee", Column("Сотрудник", width=16), EXTRA),
    ("order_id", Column("Номер заявки", width=12), EXTRA),
    ("ordered_at", Column("Заявка отправлена", Kind.DATE, 12), EXTRA),
    ("currency", Column("Валюта", width=7), EXTRA),
    ("paid_eur", Column("Итог цена, EUR", Kind.MONEY, 11), EXTRA),
    ("indexed_at", Column("Индексация проверена", Kind.DATE, 12), EXTRA),
    ("homepage", Column("Анонс на главной", width=9), EXTRA),
    # Счёт продавца (E1-14, ADR-055); счетов у размещения два — через «;».
    ("invoice", Column("Счёт", width=30), INVOICE),
    ("invoice_status", Column("Счёт: статус", width=12), INVOICE),
    ("invoice_paid", Column("Счёт оплачен", Kind.DATE, 12), INVOICE),
    ("invoice_url", Column("Счёт: ссылка на оплату", width=40), INVOICE),
)

_FIELDS = (
    "pk",
    "site_id",
    "product_id",
    "product__name",
    "site__domain",
    "seller__name",
    "article_url",
    "is_indexed",
    "status",
    "published_at",
    "comment",
    "placement_type",
    "price_paid_cents",
    "currency",
    "site__links_allowed",
    "site__marks_as_ad",
    "site__declared_topics",
    "site__languages",
    "site__site_type",
    "site__collaborator_url",
    "site__topics",
    "site__link_type",
    "employee__username",
    "employee__first_name",
    "employee__last_name",
    "collaborator_order_id",
    "ordered_at",
    "indexed_checked_at",
    "announce_on_homepage",
)

# Площадка «на сегодня» (ADR-003: отчёт — сырым SQL поверх представлений).
# «US Traff» листа — трафик США из последнего замера страны (ADR-045).
_SITES_SQL = """
SELECT l.id, l.organic_traffic, l.top_geo, l.top_geo_traffic, l.dr, l.total_keywords,
       l.placement_eur_cents,
       round(l.announce_cents / eur_rate(l.price_currency))::integer,
       l.writing_eur_cents, l.last_note, us.organic_traffic
FROM v_site_latest l
LEFT JOIN v_site_country_latest us ON us.site_id = l.id AND us.country = 'us'
WHERE l.id = ANY(%s)
"""


@dataclass(frozen=True)
class SiteToday:
    traffic: int | None
    top_geo: str | None
    top_geo_traffic: int | None
    dr: int | None
    keywords: int | None
    placement_eur_cents: int | None
    announce_eur_cents: int | None
    writing_eur_cents: int | None
    last_note: str | None
    us_traffic: int | None


@dataclass
class Total:
    count: int = 0
    eur_cents: int = 0
    without_sum: int = 0

    def add(self, eur_cents: int | None) -> None:
        self.count += 1
        if eur_cents is None:
            self.without_sum += 1
        else:
            self.eur_cents += eur_cents


@dataclass
class Totals:
    all: Total = field(default_factory=Total)
    groups: dict[tuple[str, str], Total] = field(default_factory=dict)

    def add(self, eur_cents: int | None, groups: Iterable[tuple[str, str]]) -> None:
        self.all.add(eur_cents)
        for key in groups:
            self.groups.setdefault(key, Total()).add(eur_cents)


def sheets(queryset: QuerySet[Placement], wanted: Set[str]) -> list[Sheet]:
    """Листы «Размещения» (выбранные колонки) и «Итого» по выборке списка."""
    rows = list(queryset.values(*_FIELDS))
    ids = [row["pk"] for row in rows]
    site_ids = sorted({row["site_id"] for row in rows})
    links = _links(ids)
    bills = _invoices(ids)
    sites = _sites_today(site_ids)
    articles = _published_articles(site_ids)
    rates = latest_rates()

    products_here = {row["product_id"] for row in rows}
    others = [
        (pk, name)
        for pk, name in Product.objects.order_by("pk").values_list("pk", "name")
        # Выгрузка одного продукта — примеры статей других, свой столбец не нужен.
        if not (len(products_here) == 1 and pk in products_here)
    ]
    pairs = max([SHEET_LINKS, *(len(found) for found in links.values())])
    layout = _layout(pairs, [name for _, name in others])

    totals = Totals()
    several_products = len(products_here) > 1
    table: list[list[Value]] = []
    for row in rows:
        paid = row["price_paid_cents"]
        currency = row["currency"] or "EUR"
        paid_eur = None if paid is None else to_eur_cents(paid, currency, rates)
        status = STATUS.get(row["status"], row["status"])
        service = SERVICE.get(row["placement_type"], NOT_SET) if row["placement_type"] else NOT_SET
        groups = [("Источник", row["seller__name"] or NOT_SET), ("Тип ссылки", service)]
        if several_products:
            groups.insert(0, ("Продукт", row["product__name"]))
        totals.add(paid_eur, [("Статус", status), *groups])
        site = sites.get(row["site_id"])
        examples = [
            _other_article(articles, row["site_id"], product_id, row["pk"])
            for product_id, _ in others
        ]
        found = links.get(row["pk"], [])
        cells = _row(row, site, found, pairs, examples, status, service, paid_eur)
        table.append(cells + _invoice_values(bills.get(row["pk"], [])))
    columns, values = narrow(layout, table, wanted)
    return [Sheet("Размещения", columns, list(values)), _totals_sheet(totals)]


def choices() -> list[Choice]:
    """Галочки окна выгрузки по разделам, в порядке листа."""
    found: dict[str, list[Choice]] = {}
    for key, column, group in _SPEC:
        title = column.title if column is not None else _GROUP_TITLES[key]
        found.setdefault(group, []).append(Choice(key, title, group))
    return [choice for group in found.values() for choice in group]


def _layout(pairs: int, other_products: Sequence[str]) -> list[tuple[str, Column]]:
    """Все колонки листа с ключами галочек; пары ссылок и примеры статей — по выгрузке."""
    layout: list[tuple[str, Column]] = []
    for key, column, _ in _SPEC:
        if column is not None:
            layout.append((key, column))
        elif key == LINKS:
            for number in range(1, pairs + 1):
                layout += [
                    (LINKS, Column(f"Анкор{number}", width=20)),
                    (LINKS, Column(f"Ссылка{number}", width=35)),
                ]
        else:
            layout += [
                (EXAMPLES, Column(f"Пример статьи на {name}", width=40)) for name in other_products
            ]
    return layout


def _row(
    row: dict[str, Any],
    site: SiteToday | None,
    links: list[tuple[str, str]],
    pairs: int,
    examples: list[str | None],
    status: str,
    service: str,
    paid_eur: int | None,
) -> list[Value]:
    today = site or SiteToday(None, None, None, None, None, None, None, None, None, None)
    values: list[Value] = [
        row["site__domain"],
        row["seller__name"],
        today.last_note,
        row["article_url"],
        row["is_indexed"],
        status,
        row["published_at"],
        row["published_at"],
        row["comment"],
    ]
    for number in range(pairs):
        anchor, target = links[number] if number < len(links) else (None, None)
        values += [anchor, target]
    values += examples
    values += [
        today.traffic,
        today.top_geo,
        today.top_geo_traffic,
        today.us_traffic,
        today.dr,
        today.keywords,
        None if service == NOT_SET else service,
        today.placement_eur_cents,
        today.announce_eur_cents,
        today.writing_eur_cents,
        row["price_paid_cents"],
        row["site__links_allowed"],
        row["site__marks_as_ad"],
        row["site__declared_topics"],
        row["site__languages"],
        row["site__site_type"],
        row["site__collaborator_url"],
        row["site__topics"],
        row["site__link_type"],
        row["product__name"],
        _employee(row),
        row["collaborator_order_id"],
        row["ordered_at"],
        row["currency"] if row["price_paid_cents"] is not None else None,
        paid_eur,
        row["indexed_checked_at"],
        row["announce_on_homepage"],
    ]
    return values


def _employee(row: dict[str, Any]) -> str | None:
    if not row["employee__username"]:
        return None
    name = f"{row['employee__first_name'] or ''} {row['employee__last_name'] or ''}".strip()
    return name or row["employee__username"]


def _invoices(ids: list[int]) -> dict[int, list[Invoice]]:
    """Неотменённые счета размещений, старые первыми, — одним запросом."""
    found: dict[int, list[Invoice]] = defaultdict(list)
    items = (
        InvoiceItem.objects.filter(placement_id__in=ids)
        .exclude(invoice__status=InvoiceStatus.CANCELLED)
        .select_related("invoice__seller")
        .order_by("invoice__issued_on", "invoice_id")
    )
    for item in items:
        found[item.placement_id].append(item.invoice)
    return found


def _invoice_values(bills: list[Invoice]) -> list[Value]:
    """Счёт, статус, дата оплаты (если оплачены все), ссылки на оплату."""
    if not bills:
        return [None, None, None, None]
    paid = [bill.paid_on for bill in bills if bill.status == InvoiceStatus.PAID]
    all_paid = len(paid) == len(bills) and None not in paid
    return [
        "; ".join(invoices.label(bill) for bill in bills),
        "; ".join(bill.get_status_display() for bill in bills),
        max(day for day in paid if day is not None) if all_paid else None,
        "; ".join(bill.pay_url for bill in bills if bill.pay_url) or None,
    ]


def _links(ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """Анкоры и ссылки размещений по номеру ссылки; без номера — в конце."""
    found: dict[int, list[tuple[str, str]]] = defaultdict(list)
    links = (
        PlacementLink.objects.filter(placement_id__in=ids)
        .order_by("placement_id", F("link_index").asc(nulls_last=True), "pk")
        .values_list("placement_id", "anchor", "target_url")
    )
    for placement_id, anchor, target in links:
        found[placement_id].append((anchor, target))
    return found


def _sites_today(site_ids: list[int]) -> dict[int, SiteToday]:
    with connection.cursor() as cursor:
        cursor.execute(_SITES_SQL, [site_ids])
        return {row[0]: SiteToday(*row[1:]) for row in cursor.fetchall()}


def _published_articles(site_ids: list[int]) -> dict[tuple[int, int], list[tuple[int, str]]]:
    """Опубликованные статьи площадок по продуктам, свежие первыми: «Пример статьи на …»."""
    found: dict[tuple[int, int], list[tuple[int, str]]] = defaultdict(list)
    published = (
        Placement.objects.filter(site_id__in=site_ids, status=PlacementStatus.PLACED)
        .exclude(article_url__isnull=True)
        .exclude(article_url="")
        .order_by(F("published_at").desc(nulls_last=True), "-pk")
        .values_list("pk", "site_id", "product_id", "article_url")
    )
    for pk, site_id, product_id, url in published:
        if url:
            found[(site_id, product_id)].append((pk, url))
    return found


def _other_article(
    articles: dict[tuple[int, int], list[tuple[int, str]]],
    site_id: int,
    product_id: int,
    own_pk: int,
) -> str | None:
    return next((url for pk, url in articles.get((site_id, product_id), []) if pk != own_pk), None)


def _totals_sheet(totals: Totals) -> Sheet:
    columns = (
        Column("Группа", width=12),
        Column("Значение", width=24),
        Column("Размещений", Kind.INT, 12),
        Column("Итог цена, EUR", Kind.MONEY, 14),
        Column("Без суммы", Kind.INT, 10),
    )
    rows: list[list[Value]] = [_total_row("Всего", "", totals.all)]
    order = {"Продукт": 0, "Статус": 1, "Источник": 2, "Тип ссылки": 3}
    groups = sorted(
        totals.groups.items(),
        key=lambda item: (order[item[0][0]], -item[1].eur_cents, -item[1].count, item[0][1]),
    )
    statuses = sum(1 for (group, _), _ in groups if group == "Статус")
    for (group, value), total in groups:
        if group == "Статус" and statuses == 1:
            continue  # один статус (отчёт за месяц — опубликованные) — строка не нужна
        rows.append(_total_row(group, value, total))
    return Sheet("Итого", columns, rows)


def _total_row(group: str, value: str, total: Total) -> list[Value]:
    return [group, value, total.count, total.eur_cents, total.without_sum]
