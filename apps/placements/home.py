"""Блок статистики на главной: плитки «Этот месяц» и графики по месяцам года (E1-14).

Данные — `apps.placements.stats`; здесь — то, что видит человек: подписи,
высоты столбиков в процентах, ссылки и деления оси. Выбор продукта и года —
ссылками на главную с параметрами `product` и `year`: переход без перезагрузки
делает seo/soft-nav.js. Без `product` — рабочий продукт (ADR-057), «Все продукты» —
`product=all`. Столбик — ссылка на «Размещения» за этот месяц.

Цвета графиков — переменные `--seo-chart-*` расцветок (seo/admin-palettes.css,
ADR-038): размещения — фиолетовый, траты по счетам и без счёта — пара «две
группы». Подписи значений — выборочно: текущий месяц и самый большой.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from apps.placements import invoices, stats
from apps.placements.models import Invoice, InvoiceStatus, Placement
from apps.sites.models import Product
from apps.workspace.products import ALL, working_product_id
from config.export import month_name

SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
# Предложный падеж для подписей «в сентябре».
IN_MONTH = (
    "январе",
    "феврале",
    "марте",
    "апреле",
    "мае",
    "июне",
    "июле",
    "августе",
    "сентябре",
    "октябре",
    "ноябре",
    "декабре",
)
PRODUCT_PARAM = "product"
YEAR_PARAM = "year"


@dataclass
class Bar:
    short: str
    title: str  # «Сентябрь 2026»
    url: str
    value: int
    text: str  # значение для человека
    pct: float  # высота столбика, % от верха оси
    current: bool
    labeled: bool  # подпись над столбиком: текущий месяц и самый большой
    # Части стопки: ключ цвета, доля столбика в %, сумма.
    parts: list[tuple[str, float, str]] = field(default_factory=list)


@dataclass
class Chart:
    ticks: list[tuple[str, float]]  # подпись деления, высота в %
    bars: list[Bar]

    @property
    def empty(self) -> bool:
        return not any(bar.value for bar in self.bars)


def euros(cents: int, *, approx: bool = False) -> str:
    """«€16 236»: целые евро, разряды узким пробелом; пересчёт по курсу — «≈»."""
    value = f"€{round(cents / 100):,}".replace(",", " ")
    return f"≈{value}" if approx else value


def context(request: HttpRequest) -> dict[str, Any]:
    today = timezone.localdate()
    products = list(Product.objects.filter(is_active=True).order_by("pk"))
    product = _product(request, products)
    product_id = product.pk if product else None
    first_year = _first_year(today)
    year = _year(request, today, first_year)
    months = stats.months(year, product_id)
    current, previous = stats.this_and_previous(today, product_id)

    def home(**changes: Any) -> str:
        params = {PRODUCT_PARAM: product_id or ALL, YEAR_PARAM: year, **changes}
        query = urlencode({key: value for key, value in params.items() if value is not None})
        return f"{reverse('admin:index')}?{query}" if query else reverse("admin:index")

    def placements(month: dt.date) -> str:
        # «Размещения» всегда под рабочий продукт (ADR-063), поэтому в адрес идёт
        # «Все» — не сужать: отбирает месяц. У главной счёт может быть по обоим
        # продуктам, у списка — по рабочему.
        params: dict[str, Any] = {"month": f"{month:%Y-%m}", "product__id__exact": ALL}
        return f"{reverse('admin:placements_placement_changelist')}?{urlencode(params)}"

    due = invoices.seller_money(Invoice.objects.filter(status=InvoiceStatus.ISSUED))
    in_previous = IN_MONTH[previous.month.month - 1]
    tiles = [
        {
            "label": f"Размещено в {IN_MONTH[today.month - 1]}",
            "value": str(current.placed),
            "note": f"в {in_previous}: {previous.placed}",
            "url": placements(current.month),
        },
        {
            "label": f"Потрачено в {IN_MONTH[today.month - 1]}",
            "value": euros(current.spent_cents, approx=current.converted),
            "note": f"в {in_previous}: {euros(previous.spent_cents, approx=previous.converted)}",
            "url": placements(current.month),
        },
        {
            "label": "К оплате по счетам",
            "value": invoices.sums_text(due.due) or "€0",
            "note": f"счетов: {due.due_count}",
            # Сумма — по всем счетам, список — тоже, а не по рабочему продукту.
            "url": reverse("admin:placements_invoice_changelist")
            + f"?status__exact=issued&product={ALL}",
        },
    ]
    return {
        "home_stats": {
            "products": [
                (None, "Все продукты", home(**{PRODUCT_PARAM: ALL}), product is None),
                *(
                    (p.pk, p.name, home(**{PRODUCT_PARAM: p.pk}), p.pk == product_id)
                    for p in products
                ),
            ],
            "year": year,
            "prev_year_url": home(**{YEAR_PARAM: year - 1}) if year > first_year else "",
            "next_year_url": home(**{YEAR_PARAM: year + 1}) if year < today.year else "",
            "tiles": tiles,
            "placed": _placed_chart(months, today, placements),
            "placed_sub": f"опубликовано, {year}",
            "placed_total": str(sum(m.placed for m in months)),
            "spent_total": euros(
                sum(m.spent_cents for m in months), approx=any(m.converted for m in months)
            ),
            "spent": _spent_chart(months, today, placements),
            "rows": [
                (
                    month_name(m.month).capitalize(),
                    m.placed,
                    euros(m.spent_cents, approx=m.converted),
                    euros(m.invoiced_cents),
                    euros(m.other_cents),
                )
                for m in months
            ],
            "approx": any(m.converted for m in months),
            "no_date": stats.without_date(product_id),
        }
    }


def _product(request: HttpRequest, products: list[Product]) -> Product | None:
    """Продукт статистики: из адреса, без выбора — рабочий (ADR-057), `all` — все."""
    value = request.GET.get(PRODUCT_PARAM) or ""
    if value == ALL:
        return None
    if not value:
        value = str(working_product_id(request) or "")
    return next((p for p in products if str(p.pk) == value), None)


def _first_year(today: dt.date) -> int:
    """Год самой ранней публикации: раньше листать незачем."""
    first = (
        Placement.objects.filter(published_at__isnull=False)
        .order_by("published_at")
        .values_list("published_at", flat=True)
        .first()
    )
    return timezone.localdate(first).year if first else today.year


def _year(request: HttpRequest, today: dt.date, first_year: int) -> int:
    value = request.GET.get(YEAR_PARAM) or ""
    if value.isdigit() and first_year <= int(value) <= today.year:
        return int(value)
    return today.year


def _labeled(values: list[int], index: int, current: int | None) -> bool:
    top = max(values)
    return values[index] > 0 and (index == current or values[index] == top)


def _current(months: list[stats.Month], today: dt.date) -> int | None:
    return today.month - 1 if months and months[0].month.year == today.year else None


def _axis(top: int, label: Any) -> tuple[int, list[tuple[str, float]]]:
    if top <= 0:
        # Пустой год: одна линия нуля, столбиков нет.
        return 1, [(label(0), 0.0)]
    ticks = stats.nice_ticks(top)
    peak = ticks[-1]
    return peak, [(label(tick), tick * 100 / peak) for tick in ticks]


def _placed_chart(months: list[stats.Month], today: dt.date, link: Any) -> Chart:
    values = [m.placed for m in months]
    peak, ticks = _axis(max(values), str)
    current = _current(months, today)
    bars = [
        Bar(
            short=SHORT[index],
            title=month_name(m.month).capitalize(),
            url=link(m.month),
            value=m.placed,
            text=str(m.placed),
            pct=m.placed * 100 / peak,
            current=index == current,
            labeled=_labeled(values, index, current),
        )
        for index, m in enumerate(months)
    ]
    return Chart(ticks, bars)


def _spent_chart(months: list[stats.Month], today: dt.date, link: Any) -> Chart:
    values = [m.spent_cents for m in months]
    peak, ticks = _axis(max(values), euros)
    current = _current(months, today)
    bars = []
    for index, m in enumerate(months):
        bar = Bar(
            short=SHORT[index],
            title=month_name(m.month).capitalize(),
            url=link(m.month),
            value=m.spent_cents,
            text=euros(m.spent_cents, approx=m.converted),
            pct=m.spent_cents * 100 / peak,
            current=index == current,
            labeled=_labeled(values, index, current),
        )
        # Части столбика снизу вверх — по счетам, без счёта; высота части — доля
        # столбика. Пустая часть не рисуется: между частями зазор 2px.
        bar.parts = [
            (key, cents * 100 / m.spent_cents, euros(cents))
            for key, cents in (("invoiced", m.invoiced_cents), ("other", m.other_cents))
            if cents
        ]
        bars.append(bar)
    return Chart(ticks, bars)
