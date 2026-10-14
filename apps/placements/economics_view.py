"""Экран «Экономика» (Работа → Экономика): бюджет, траты, план, размещённые ссылки.

Сверху — продукт и период (месяц, квартал, год или свой «с — по»): ссылками
и формой с адресом, переход без перезагрузки делает seo/soft-nav.js. Ниже —
плитки периода и полоса бюджета, два графика по месяцам всего срока работы
(деньги столбиками с линией бюджета, ссылки линией с числом у каждой точки —
как в таблице «Линкбилдинг» пользователя), разбивки плана и «что не
посчитано», таблица по месяцам и размещения периода. Тот же отчёт —
файлом Excel и отдельной HTML-страницей (`export=xlsx|html`).

Данные — `apps.placements.economics`; здесь — то, что видит человек. Графики —
SVG, геометрию считает Python: страница и HTML-файл рисуют их одним шаблоном.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.contrib import admin
from django.contrib.staticfiles import finders
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db.models import F
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from apps.content.domain_settings import monthly_budget
from apps.content.models import DomainSetting
from apps.placements import economics, stats
from apps.placements.economics import Month, Period, Report, add_months
from apps.placements.home import IN_MONTH, SHORT
from apps.placements.models import PlacementStatus
from apps.sites.models import Product
from apps.workspace.products import ALL, working_product_id
from config.export import MONTHS, attachment, month_name

PRODUCT_PARAM = "product"
PERIOD_PARAM = "period"
AT_PARAM = "at"
FROM_PARAM = "from"
TO_PARAM = "to"
EXPORT_PARAM = "export"

MONTH, QUARTER, YEAR, RANGE = "month", "quarter", "year", "range"
PERIODS = ((MONTH, "Месяц"), (QUARTER, "Квартал"), (YEAR, "Год"), (RANGE, "Свой период"))
STEPS = {MONTH: 1, QUARTER: 3, YEAR: 12}
# Свой период — не длиннее трёх лет: дальше и графики, и таблица теряют смысл.
RANGE_LIMIT = 36
ROMAN = ("I", "II", "III", "IV")

STATUS_LABELS = {status.value: str(status.label) for status in PlacementStatus}


# --- Деньги и числа ---


def euros(cents: int, *, approx: bool = False) -> str:
    """«€16 236», «−€1 200»: целые евро, разряды узким пробелом; по курсу — «≈»."""
    sign = "−" if cents < 0 else ""
    value = f"{sign}€{round(abs(cents) / 100):,}".replace(",", " ")
    return f"≈{value}" if approx else value


def compact(cents: int) -> str:
    """Подпись над столбиком: «€42,7k», «€930»."""
    value = abs(cents) / 100
    sign = "−" if cents < 0 else ""
    if value >= 1000:
        text = f"{value / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{sign}€{text}k"
    return f"{sign}€{round(value)}"


def share(part: int, whole: int) -> str:
    """Доля в процентах для подписи: «12%»; без целого — пусто."""
    return f"{round(part * 100 / whole)}%" if whole > 0 else ""


def of_budget(part: int, budget: int) -> str:
    """«12% бюджета»; бюджета нет — пусто."""
    value = share(part, budget)
    return f"{value} бюджета" if value else ""


def count_text(value: int, forms: tuple[str, str, str]) -> str:
    """«1 размещение», «3 размещения», «5 размещений»."""
    tail = value % 100
    if 11 <= tail <= 14:
        form = forms[2]
    else:
        form = {1: forms[0], 2: forms[1], 3: forms[1], 4: forms[1]}.get(value % 10, forms[2])
    return f"{value:,}".replace(",", " ") + f" {form}"


PLACEMENTS_FORMS = ("размещение", "размещения", "размещений")
LINKS_FORMS = ("ссылка", "ссылки", "ссылок")


def month_title(month: dt.date) -> str:
    return month_name(month).capitalize()


# --- Параметры экрана ---


@dataclass(frozen=True)
class State:
    """Что выбрано на экране: продукт (None — все) и период."""

    products: list[Product]
    product: Product | None
    kind: str
    period: Period
    today: dt.date

    @property
    def current(self) -> dt.date:
        return self.today.replace(day=1)

    @property
    def product_id(self) -> int | None:
        return self.product.pk if self.product is not None else None

    @property
    def label(self) -> str:
        start, last = self.period.start, add_months(self.period.end, -1)
        if self.kind == MONTH:
            return month_title(start)
        if self.kind == QUARTER:
            return f"{ROMAN[(start.month - 1) // 3]} квартал {start.year}"
        if self.kind == YEAR:
            return f"{start.year} год"
        if start == last:
            return month_title(start)
        if start.year == last.year:
            return f"{month_title(start).split()[0]} — {month_name(last)}"
        return f"{month_title(start)} — {month_name(last)}"

    @property
    def product_label(self) -> str:
        return self.product.name if self.product is not None else "Все продукты"

    def params(self, **changes: Any) -> dict[str, str]:
        """Параметры адреса экрана; `changes` — что поменять."""
        params: dict[str, Any] = {
            PRODUCT_PARAM: self.product_id or ALL,
            PERIOD_PARAM: self.kind,
        }
        if self.kind == RANGE:
            params[FROM_PARAM] = f"{self.period.start:%Y-%m}"
            params[TO_PARAM] = f"{add_months(self.period.end, -1):%Y-%m}"
        else:
            params[AT_PARAM] = f"{self.period.start:%Y-%m}"
        params.update(changes)
        if params.get(PERIOD_PARAM) != RANGE:
            params.pop(FROM_PARAM, None)
            params.pop(TO_PARAM, None)
        else:
            params.pop(AT_PARAM, None)
        return {key: str(value) for key, value in params.items() if value is not None}

    def url(self, **changes: Any) -> str:
        return f"{reverse('admin:economics')}?{urlencode(self.params(**changes))}"


def parse_month(value: str | None) -> dt.date | None:
    try:
        return dt.datetime.strptime(value or "", "%Y-%m").date()
    except ValueError:
        return None


def state_from(request: HttpRequest, today: dt.date | None = None) -> State:
    today = today or timezone.localdate()
    current = today.replace(day=1)
    products = list(Product.objects.filter(is_active=True).order_by("pk"))
    value = request.GET.get(PRODUCT_PARAM) or ""
    product: Product | None
    if value == ALL:
        product = None
    else:
        value = value or str(working_product_id(request) or "")
        product = next((p for p in products if str(p.pk) == value), None)
    kind = request.GET.get(PERIOD_PARAM) or MONTH
    if kind not in dict(PERIODS):
        kind = MONTH
    if kind == RANGE:
        start = parse_month(request.GET.get(FROM_PARAM)) or add_months(current, -5)
        last = parse_month(request.GET.get(TO_PARAM)) or current
        if last < start:
            start, last = last, start
        start = max(start, add_months(last, -(RANGE_LIMIT - 1)))
        return State(products, product, kind, Period(start, add_months(last, 1)), today)
    at = parse_month(request.GET.get(AT_PARAM)) or current
    if kind == QUARTER:
        at = dt.date(at.year, (at.month - 1) // 3 * 3 + 1, 1)
    elif kind == YEAR:
        at = dt.date(at.year, 1, 1)
    return State(products, product, kind, Period(at, add_months(at, STEPS[kind])), today)


# --- Графики: геометрия SVG ---

# Размер — как у графика на экране 1920 px (две карточки рядом): подписи в SVG
# растягиваются вместе с ним, и 12 единиц здесь — около 12 px там.
WIDTH, HEIGHT = 680, 290
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 56, 14, 24, 40
PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT
PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM
GAP = 2  # зазор цвета фона между частями столбика
RADIUS = 3  # скругление верха столбика


@dataclass
class Slot:
    """Место месяца по оси X: подпись, область наведения, подсказка."""

    x: float
    w: float
    cx: float
    short: str
    year: str  # год под январём и первым месяцем
    in_period: bool
    current: bool
    tip: str


@dataclass
class Segment:
    key: str  # spent | planned
    path: str


@dataclass
class Column:
    slot: Slot
    segments: list[Segment]
    label: str  # подпись над столбиком — у месяцев периода
    label_y: float


@dataclass
class Point:
    slot: Slot
    cy: float
    value: int
    labeled: bool


@dataclass
class Chart:
    width: int = WIDTH
    height: int = HEIGHT
    left: int = PAD_LEFT
    right: int = WIDTH - PAD_RIGHT
    top: int = PAD_TOP
    bottom: int = HEIGHT - PAD_BOTTOM
    plot_h: int = PLOT_H
    tick_x: int = PAD_LEFT - 8  # подписи оси Y — правым краем у сетки
    month_y: int = HEIGHT - PAD_BOTTOM + 17
    year_y: int = HEIGHT - PAD_BOTTOM + 33
    ticks: list[tuple[str, float]] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    band: tuple[float, float] | None = None  # период: x и ширина подложки
    empty: bool = False
    # Деньги
    columns: list[Column] = field(default_factory=list)
    budget_y: float | None = None
    budget_label: str = ""
    # Ссылки
    points: list[Point] = field(default_factory=list)
    line: str = ""


def _y(value: float, peak: float) -> float:
    return PAD_TOP + PLOT_H * (1 - value / peak)


def _ticks(top: int, label: Any) -> tuple[int, list[tuple[str, float]]]:
    if top <= 0:
        return 1, [(label(0), _y(0, 1))]
    ticks = stats.nice_ticks(top)
    peak = ticks[-1]
    return peak, [(label(tick), _y(tick, peak)) for tick in ticks]


def _slots(months: list[Month], period: Period, current: dt.date, tips: list[str]) -> list[Slot]:
    width = PLOT_W / max(len(months), 1)
    slots = []
    for index, month in enumerate(months):
        x = PAD_LEFT + index * width
        slots.append(
            Slot(
                x=x,
                w=width,
                cx=x + width / 2,
                short=SHORT[month.month.month - 1],
                year=str(month.month.year) if index == 0 or month.month.month == 1 else "",
                in_period=month.month in period,
                current=month.month == current,
                tip=tips[index],
            )
        )
    return slots


def _band(slots: list[Slot]) -> tuple[float, float] | None:
    chosen = [slot for slot in slots if slot.in_period]
    if not chosen or len(chosen) == len(slots):
        return None
    return chosen[0].x, chosen[-1].x + chosen[-1].w - chosen[0].x


def _rounded_top(x: float, y: float, w: float, h: float) -> str:
    """Столбик со скруглённым верхом и прямым низом — у оси."""
    r = min(RADIUS, w / 2, h)
    return (
        f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z"
    )


def _square(x: float, y: float, w: float, h: float) -> str:
    return f"M{x:.1f},{y + h:.1f}V{y:.1f}H{x + w:.1f}V{y + h:.1f}Z"


def money_tip(month: Month) -> str:
    lines = [
        month_title(month.month),
        f"Потрачено: {euros(month.spent_cents, approx=month.converted)}",
        f"Запланировано: {euros(month.planned_cents)}",
        f"Всего: {euros(month.total_cents)} из {euros(month.budget_cents)}",
        ("Перерасход: " if month.left_cents < 0 else "Осталось: ") + euros(abs(month.left_cents)),
    ]
    return "\n".join(lines)


def links_tip(month: Month, employees: list[str]) -> str:
    lines = [month_title(month.month), f"Размещено: {count_text(month.placed, LINKS_FORMS)}"]
    for name in employees:
        count = month.by_employee.get(name, 0)
        if count:
            lines.append(f"{name or 'без сотрудника'}: {count}")
    if month.average_cents is not None:
        lines.append(f"Средняя цена: {euros(month.average_cents)}")
    if month.planned:
        lines.append(f"В плане: {count_text(month.planned, PLACEMENTS_FORMS)}")
    return "\n".join(lines)


def money_chart(report: Report) -> Chart:
    months = report.months
    budget = report.budget_month_cents
    top = max([month.total_cents for month in months] + [budget])
    peak, ticks = _ticks(top, euros)
    slots = _slots(months, report.period, report.current, [money_tip(month) for month in months])
    chart = Chart(ticks=ticks, slots=slots, band=_band(slots))
    chart.empty = not any(month.total_cents for month in months)
    bar = min(28.0, slots[0].w * 0.62) if slots else 0.0
    # Подписи над столбиками — выборочно: у месяцев периода, если места хватает,
    # иначе только у текущего и самого большого; остальное — подсказка и таблица.
    roomy = slots[0].w >= 44 if slots else False
    highest = max((month.total_cents for month in months), default=0)
    for slot, month in zip(slots, months, strict=True):
        x = slot.cx - bar / 2
        base = float(chart.bottom)
        segments = []
        parts = [
            (key, cents)
            for key, cents in (("spent", month.spent_cents), ("planned", month.planned_cents))
            if cents > 0
        ]
        for index, (key, cents) in enumerate(parts):
            height = PLOT_H * cents / peak
            gap = GAP if index > 0 else 0
            y = base - height
            visible = max(height - gap, 1.0)
            last = index == len(parts) - 1
            draw = _rounded_top if last else _square
            segments.append(Segment(key, draw(x, y, bar, visible)))
            base = y
        chart.columns.append(
            Column(
                slot=slot,
                segments=segments,
                label=compact(month.total_cents)
                if month.total_cents
                and (slot.current or month.total_cents == highest or (roomy and slot.in_period))
                else "",
                label_y=base - 6,
            )
        )
    if budget > 0:
        chart.budget_y = _y(budget, peak)
        chart.budget_label = f"бюджет {euros(budget)}"
    return chart


def links_chart(report: Report) -> Chart:
    months = [month for month in report.months if month.month <= report.current]
    values = [month.placed for month in months]
    peak, ticks = _ticks(max(values, default=0), str)
    slots = _slots(
        report.months,
        report.period,
        report.current,
        [links_tip(month, report.employees) for month in report.months],
    )
    chart = Chart(ticks=ticks, slots=slots, band=_band(slots))
    chart.empty = not any(values)
    labeled = slots[0].w >= 18 if slots else False
    for slot, month in zip(slots, months, strict=False):
        chart.points.append(
            Point(slot=slot, cy=_y(month.placed, peak), value=month.placed, labeled=labeled)
        )
    if len(chart.points) > 1:
        chart.line = "M" + "L".join(f"{p.slot.cx:.1f},{p.cy:.1f}" for p in chart.points)
    return chart


# --- Плитки, разбивки, таблицы ---


def _budget_setting_url(product_id: int) -> str:
    """Где поменять бюджет: значение продукта, если оно своё, иначе общее."""
    rows = DomainSetting.objects.filter(key="MONTHLY_BUDGET")
    rows = rows.filter(product_id=product_id) | rows.filter(product__isnull=True)
    found = rows.order_by(F("product_id").asc(nulls_last=True)).values_list("pk", flat=True).first()
    if found is None:
        return reverse("admin:content_domainsetting_changelist")
    return reverse("admin:content_domainsetting_change", args=[found])


def tiles(state: State, report: Report) -> list[dict[str, Any]]:
    total = report.total
    months = len(report.period.months)
    budget_note = f"{euros(report.budget_month_cents)} в месяц"
    if months > 1:
        budget_note += f" × {months}"
    if state.product is None and len(report.budgets) > 1:
        budget_note = " + ".join(f"{name} {euros(cents)}" for name, cents in report.budgets)
        budget_note += " в месяц" + (f" × {months}" if months > 1 else "")
    left = total.left_cents
    unspent = total.budget_cents - total.spent_cents
    placed_note = (
        f"средняя цена {euros(total.average_cents)}"
        if total.average_cents is not None
        else "сумм нет"
    )
    return [
        {
            "key": "budget",
            "label": "Бюджет",
            "value": euros(total.budget_cents),
            "note": budget_note,
            "link": _budget_setting_url(state.product.pk) if state.product is not None else "",
            "link_text": "изменить",
        },
        {
            "key": "spent",
            "label": "Потрачено",
            "value": euros(total.spent_cents, approx=total.converted),
            "note": " · ".join(
                part
                for part in (
                    count_text(total.placed, LINKS_FORMS),
                    of_budget(total.spent_cents, total.budget_cents),
                )
                if part
            ),
            "filter": "spent",
        },
        {
            "key": "planned",
            "label": "Запланировано",
            "value": euros(total.planned_cents),
            "note": " · ".join(
                part
                for part in (
                    count_text(total.planned, PLACEMENTS_FORMS),
                    of_budget(total.planned_cents, total.budget_cents),
                )
                if part
            ),
            "filter": "planned",
        },
        {
            "key": "left",
            "label": "Перерасход" if left < 0 else "Осталось",
            "value": euros(left),
            "note": "потрачено и запланировано больше бюджета"
            if left < 0
            else f"бюджет − потрачено − план · не потрачено {euros(unspent)}",
            "over": left < 0,
        },
        {
            "key": "placed",
            "label": "Размещено ссылок",
            "value": f"{total.placed:,}".replace(",", " "),
            "note": placed_note,
            "filter": "spent",
        },
    ]


def budget_bar(report: Report) -> dict[str, Any]:
    """Полоса бюджета: потрачено, запланировано, свободно; перерасход — за отметкой."""
    total = report.total
    budget = total.budget_cents
    used = total.spent_cents + total.planned_cents
    scale = max(budget, used, 1)
    free = max(budget - used, 0)
    return {
        "spent": total.spent_cents * 100 / scale,
        "planned": total.planned_cents * 100 / scale,
        "free": free * 100 / scale,
        "mark": budget * 100 / scale if used > budget and budget > 0 else None,
        "over": used > budget,
        "legend": [
            (
                "spent",
                "Потрачено",
                euros(total.spent_cents, approx=total.converted),
                share(total.spent_cents, budget),
            ),
            (
                "planned",
                "Запланировано",
                euros(total.planned_cents),
                share(total.planned_cents, budget),
            ),
            ("free", "Свободно", euros(free), share(free, budget)),
        ],
        "over_text": f"перерасход {euros(used - budget)}" if used > budget else "",
    }


def breakdowns(report: Report) -> dict[str, Any]:
    statuses = report.by_status
    top_status = max([group.cents for group in statuses] + [1])
    sellers = report.by_seller
    shown = sellers[:6]
    rest = sellers[6:]
    if rest:
        shown.append(
            economics.Group(
                f"другие ({len(rest)})",
                sum(group.count for group in rest),
                sum(group.cents for group in rest),
                spent_cents=sum(group.spent_cents for group in rest),
                planned_cents=sum(group.planned_cents for group in rest),
            )
        )
    top_seller = max([group.cents for group in shown] + [1])
    return {
        "statuses": [
            {
                "name": group.name,
                "count": group.count,
                "sum": euros(group.cents),
                "pct": group.cents * 100 / top_status,
            }
            for group in statuses
        ],
        "sellers": [
            {
                "name": group.name,
                "count": group.count,
                "sum": euros(group.cents),
                "spent": group.spent_cents * 100 / top_seller,
                "planned": group.planned_cents * 100 / top_seller,
                "tip": f"{group.name}: потрачено {euros(group.spent_cents)}, "
                f"запланировано {euros(group.planned_cents)}",
            }
            for group in shown
        ],
    }


def issues(state: State, report: Report) -> list[dict[str, Any]]:
    """«Что не посчитано»: числом, с объяснением и переходом к строкам."""
    found = []
    if report.no_price:
        found.append(
            {
                "count": report.no_price,
                "text": "в плане без суммы — нет рабочей цены площадки или порога "
                "написания у продукта",
                "filter": "noprice",
            }
        )
    if report.no_order_date:
        found.append(
            {
                "count": report.no_order_date,
                "text": "в плане без даты отправки заявки — посчитаны текущим месяцем",
                "filter": "nodate",
            }
        )
    if report.no_paid:
        found.append(
            {
                "count": report.no_paid,
                "text": "размещено без «Заплачено» — в «Потрачено» не вошли",
                "filter": "nopaid",
            }
        )
    if report.no_published:
        params = {"month": "none", "status__exact": PlacementStatus.PLACED.value}
        params["product__id__exact"] = str(state.product_id or ALL)
        found.append(
            {
                "count": report.no_published,
                "text": "размещено без даты публикации — ни в один месяц не попали",
                "url": f"{reverse('admin:placements_placement_changelist')}?{urlencode(params)}",
            }
        )
    return found


def month_rows(report: Report) -> dict[str, Any]:
    """Таблица по месяцам — как лист «Линкбилдинг»: старые сверху, итог внизу."""
    rows = []
    for month in report.months:
        rows.append(
            {
                "title": month_title(month.month),
                "in_period": month.month in report.period,
                "current": month.month == report.current,
                "placed": month.placed,
                "employees": [month.by_employee.get(name, 0) for name in report.employees],
                "spent": euros(month.spent_cents, approx=month.converted),
                "average": euros(month.average_cents) if month.average_cents is not None else "—",
                "planned": euros(month.planned_cents) if month.planned_cents else "—",
                "total": euros(month.total_cents),
                "budget": euros(month.budget_cents),
                "left": euros(month.left_cents),
                "over": month.left_cents < 0,
            }
        )
    total = Month(report.period.start)
    for month in report.months:
        total.placed += month.placed
        total.priced += month.priced
        total.spent_cents += month.spent_cents
        total.planned_cents += month.planned_cents
        total.planned += month.planned
        total.budget_cents += month.budget_cents
        total.converted = total.converted or month.converted
        for name, count in month.by_employee.items():
            total.by_employee[name] = total.by_employee.get(name, 0) + count
    return {
        "employees": [name or "без сотрудника" for name in report.employees],
        "rows": rows,
        "total": {
            "placed": total.placed,
            "employees": [total.by_employee.get(name, 0) for name in report.employees],
            "spent": euros(total.spent_cents, approx=total.converted),
            "average": euros(total.average_cents) if total.average_cents is not None else "—",
            "planned": euros(total.planned_cents),
            "total": euros(total.total_cents),
            "budget": euros(total.budget_cents),
            "left": euros(total.left_cents),
            "over": total.left_cents < 0,
        },
    }


def _day(value: dt.datetime | None) -> str:
    return f"{timezone.localtime(value):%d.%m.%Y}" if value is not None else ""


def placement_rows(report: Report) -> list[dict[str, Any]]:
    result = []
    for row in report.rows:
        flags = []
        if not row.spent and row.eur_cents is None:
            flags.append("noprice")
        if not row.spent and row.ordered_at is None:
            flags.append("nodate")
        if row.spent and row.eur_cents is None:
            flags.append("nopaid")
        if row.eur_cents is None:
            source = "нет суммы"
        elif row.from_paid:
            source = "заплачено" if row.spent else "договорено"
        else:
            source = "по рабочей цене"
        result.append(
            {
                "row": row,
                "kind": "spent" if row.spent else "planned",
                "flags": " ".join(flags),
                "status": STATUS_LABELS.get(row.status, row.status),
                "ordered": _day(row.ordered_at),
                "published": _day(row.published_at),
                "sum": euros(row.eur_cents, approx=row.converted)
                if row.eur_cents is not None
                else "—",
                "source": source,
                "url": reverse("admin:placements_placement_change", args=[row.id]),
            }
        )
    return result


def chips(report: Report) -> list[tuple[str, str, int]]:
    rows = report.rows
    spent = sum(1 for row in rows if row.spent)
    return [
        (key, label, count)
        for key, label, count in (
            ("all", "Все", len(rows)),
            ("spent", "Потрачено", spent),
            ("planned", "Запланировано", len(rows) - spent),
            ("noprice", "Без суммы в плане", report.no_price),
            ("nodate", "Без даты отправки", report.no_order_date),
            ("nopaid", "Размещено без «Заплачено»", report.no_paid),
        )
        if key in ("all", "spent", "planned") or count
    ]


def context(state: State, report: Report) -> dict[str, Any]:
    step = STEPS.get(state.kind)
    prev_url = next_url = ""
    if step:
        prev_url = state.url(**{AT_PARAM: f"{add_months(state.period.start, -step):%Y-%m}"})
        following = add_months(state.period.start, step)
        if following <= state.current:
            next_url = state.url(**{AT_PARAM: f"{following:%Y-%m}"})
    is_all = state.product is None
    return {
        "econ": {
            "state": state,
            "label": state.label,
            "product_label": state.product_label,
            "is_all": is_all,
            "products": [
                ("Все продукты", state.url(**{PRODUCT_PARAM: ALL}), is_all),
                *(
                    (p.name, state.url(**{PRODUCT_PARAM: p.pk}), p == state.product)
                    for p in state.products
                ),
            ],
            "periods": [
                (key, label, state.url(**{PERIOD_PARAM: key}), key == state.kind)
                for key, label in PERIODS
            ],
            "prev_url": prev_url,
            "next_url": next_url,
            "today_url": state.url(**{AT_PARAM: f"{state.current:%Y-%m}"})
            if step and state.current not in state.period
            else "",
            "range": state.kind == RANGE,
            "range_from": f"{state.period.start:%Y-%m}",
            "range_to": f"{add_months(state.period.end, -1):%Y-%m}",
            "range_max": f"{state.current:%Y-%m}",
            "hidden": [
                (key, value)
                for key, value in state.params().items()
                if key in (PRODUCT_PARAM, PERIOD_PARAM)
            ],
            "tiles": tiles(state, report),
            "bar": budget_bar(report),
            "money": money_chart(report),
            "links": links_chart(report),
            "chart_range": f"{month_title(report.months[0].month)} — "
            f"{month_name(report.months[-1].month)}"
            if report.months
            else "",
            **breakdowns(report),
            "issues": issues(state, report),
            "table": month_rows(report),
            "placements": placement_rows(report),
            "chips": chips(report),
            "approx": report.total.converted or any(m.converted for m in report.months),
            "export_xlsx": state.url(**{EXPORT_PARAM: "xlsx"}),
            "export_html": state.url(**{EXPORT_PARAM: "html"}),
            "generated": timezone.localtime(),
        }
    }


# --- Экран и выгрузки ---


def view(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm("placements.view_placement"):
        raise PermissionDenied
    state = state_from(request)
    products = (
        [(state.product.pk, state.product.name)]
        if state.product is not None
        else [(p.pk, p.name) for p in state.products]
    )
    report = economics.report(state.period, products, state.product_id, state.today)
    export = request.GET.get(EXPORT_PARAM)
    if export == "xlsx":
        from apps.placements import economics_export

        return attachment(
            economics_export.xlsx(state, report),
            f"{file_name(state)}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    data = context(state, report)
    if export == "html":
        html = render_to_string(
            "admin/placements/economics/export.html",
            {
                **data,
                "inline_css": _static_text("seo/economics.css"),
                "inline_js": _static_text("seo/economics.js"),
            },
            request=request,
        )
        return attachment(html.encode(), f"{file_name(state)}.html", "text/html; charset=utf-8")
    return TemplateResponse(
        request,
        "admin/placements/economics/page.html",
        {**admin.site.each_context(request), **data, "title": "Экономика"},
    )


def file_name(state: State) -> str:
    return f"Экономика {state.product_label} — {state.label}"


def _static_text(path: str) -> str:
    """Наш CSS или скрипт целиком — внутрь HTML-файла отчёта: открывается без системы."""
    found = finders.find(path)
    if not isinstance(found, str):
        return ""
    with open(found, encoding="utf-8") as handle:
        return handle.read()


def home_card(product: Product | None, today: dt.date | None = None) -> tuple[str, str]:
    """Карточка «Экономика» на главной: сколько осталось в этом месяце."""
    today = today or timezone.localdate()
    current = today.replace(day=1)
    if product is None:
        return "", ""
    try:
        budget = monthly_budget(product.pk)
    except ImproperlyConfigured:
        return "", "бюджет не задан в «Настройках»"
    report = economics.report(
        Period(current, add_months(current, 1)), [(product.pk, product.name)], product.pk, today
    )
    left = report.total.left_cents
    month = month_name(current).split()[0]
    if not budget.eur_cents:
        return euros(report.total.total_cents), f"потрачено и в плане · {product.name}"
    if left < 0:
        return euros(left), f"перерасход в {_in_month(month)} · {product.name}"
    return euros(left), f"осталось в {_in_month(month)} · {product.name}"


def _in_month(nominative: str) -> str:
    return IN_MONTH[MONTHS.index(nominative)]
