"""Статус площадки в списках и карточке — по её размещениям (ADR-073).

В «Площадках» и «Размещениях» колонка «статус» — фишки размещений рабочего
продукта на площадке: полное название статуса и когда его поставили; фишка
открывает своё размещение панелью. Закрытых много — одной фишкой на статус
(«Размещено ×8»). Без размещений — решение по площадке (Новая, Просмотрено,
Отбрасываю…) с датой. «Больше не работаем» (Отбрасываю, Отказ, Чёрный список)
при размещениях — красной фишкой после них. В карточке площадки — те же
фишки у каждого продукта, за всё время.

Над списком — быстрые фильтры (`StatusPeriodFilter`): статус кнопками и
«Период ▾» — месяц, 3 месяца, год, всё время или свой, с месяца по месяц.
Период — по дате, когда поставлен выбранный статус: у размещения — его, у
площадки без размещений — её решение; «Все» с периодом — всё, что поставлено
за период. Закрытые размещения старше периода из ячейки прячутся, идущие видны
всегда.

Дата статуса размещения — `repeat.status_at`, решения по площадке —
`decision_at`: последняя смена на нынешний статус в истории (ADR-049), иначе
дата из самой записи.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.contrib import admin
from django.db.models import DateTimeField, Exists, F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from apps.placements import repeat
from apps.placements.models import Placement
from apps.sites.models import ProductSite, SiteStatusChange, WorkStatus

# Готовые периоды: ключ, подпись, сколько дней назад.
PRESETS: tuple[tuple[str, str, int], ...] = (
    ("m1", "Месяц", 31),
    ("m3", "3 месяца", 92),
    ("y1", "Год", 366),
)
ALL_TIME = "Всё время"
# Решения «больше не работаем»: при размещениях — красной фишкой после них.
STOPS = (WorkStatus.DISCARDED, WorkStatus.REJECTED, WorkStatus.BLACKLISTED)
# Больше фишек за период — закрытые собираются по статусу: «Размещено ×8».
MANY = 3
MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


# ---------- Период ----------


@dataclass(frozen=True)
class Period:
    """Выбранный период: ключ для адреса, границы и подпись кнопки."""

    key: str = ""
    start: datetime | None = None
    end: datetime | None = None  # не включая
    label: str = ALL_TIME
    # Свой период — первый и последний месяц (`None` — граница открыта).
    own: tuple[date | None, date | None] | None = None

    @property
    def active(self) -> bool:
        return self.start is not None or self.end is not None

    def holds(self, moment: datetime | None) -> bool:
        if not self.active:
            return True
        if moment is None:
            return False
        if self.start is not None and moment < self.start:
            return False
        return not (self.end is not None and moment >= self.end)


def parse_period(
    value: str | None, start: str | None = None, end: str | None = None, now: datetime | None = None
) -> Period:
    """Готовый период (`m1`, `m3`, `y1`) или свой — с месяца `start` по месяц `end`
    («2026-09»); одна граница — открытый с другой стороны. Пусто или мусор — всё время."""
    now = now or timezone.now()
    first, last = _month(start), _month(end)
    if first is not None or last is not None:
        if first is not None and last is not None and last < first:
            first, last = last, first
        begin = _month_start(first.year, first.month) if first is not None else None
        finish = (
            _month_start(last.year + last.month // 12, last.month % 12 + 1)
            if last is not None
            else None
        )
        return Period("own", begin, finish, _own_label(first, last), (first, last))
    value = (value or "").strip()
    for key, label, days in PRESETS:
        if value == key:
            return Period(key, now - timedelta(days=days), None, label.lower())
    return Period()


def _month(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m").date()
    except ValueError:
        return None


def _own_label(first: date | None, last: date | None) -> str:
    """«сентябрь 2026», «09.2026 — 10.2026», «с 09.2026», «по 10.2026»."""
    if first is not None and last is not None and first == last:
        return f"{MONTHS[first.month - 1]} {first.year}"
    if first is not None and last is not None:
        return f"{first:%m.%Y} — {last:%m.%Y}"
    if first is not None:
        return f"с {first:%m.%Y}"
    assert last is not None
    return f"по {last:%m.%Y}"


def _month_start(year: int, month: int) -> datetime:
    return timezone.make_aware(datetime(year, month, 1))


def month_value(moment: datetime | date | None) -> str:
    """Значение поля «месяц» (`<input type=month>`): «2026-09»."""
    return f"{moment:%Y-%m}" if moment is not None else ""


# ---------- Отбор по статусу и периоду ----------


def decision_at(created: Any = None) -> Coalesce:
    """Когда поставлено решение по площадке — выражение для запроса по её строке.

    Последняя смена на нынешний статус в истории (ADR-049); её нет — когда
    строку завели. Строка — `product_sites` или `v_product_site_latest`: у
    представления своего `created_at` нет, его передают подзапросом (`created`).
    """
    changed = (
        SiteStatusChange.objects.filter(
            product_site_id=OuterRef("pk"), to_status=OuterRef("status")
        )
        .order_by("-changed_at", "-pk")
        .values("changed_at")[:1]
    )
    return Coalesce(
        Subquery(changed),
        created if created is not None else F("created_at"),
        output_field=DateTimeField(),
    )


def _in_period(name: str, period: Period) -> Q:
    q = Q()
    if period.start is not None:
        q &= Q(**{f"{name}__gte": period.start})
    if period.end is not None:
        q &= Q(**{f"{name}__lt": period.end})
    return q


def filter_sites(queryset: QuerySet[Any], status: str | None, period: Period) -> QuerySet[Any]:
    """«Площадки»: выбранный статус (или любой) поставлен в период.

    Свой статус площадки считается, пока размещений нет, и «больше не работаем»
    при размещениях; иначе — статусы размещений продукта на площадке.
    """
    if not status and not period.active:
        return queryset
    placements = Placement.objects.filter(
        site_id=OuterRef("site_id"), product_id=OuterRef("product_id")
    )
    # alias, а не annotate: даты нужны только для отбора, в выборку они не идут.
    deals = placements.alias(at=repeat.status_at()).filter(_in_period("at", period))
    if status:
        deals = deals.filter(status=status)
    own = (~Exists(placements) | Q(status__in=STOPS)) & _in_period("decided_at", period)
    if status:
        own &= Q(status=status)
    created = Subquery(ProductSite.objects.filter(pk=OuterRef("pk")).values("created_at")[:1])
    found: QuerySet[Any] = queryset.alias(decided_at=decision_at(created)).filter(
        Exists(deals) | own
    )
    return found


def filter_placements(
    queryset: QuerySet[Placement], status: str | None, period: Period
) -> QuerySet[Placement]:
    """«Размещения»: размещения с выбранным статусом (или любым), поставленным в период."""
    if not status and not period.active:
        return queryset
    queryset = queryset.alias(status_at=repeat.status_at()).filter(_in_period("status_at", period))
    return queryset.filter(status=status) if status else queryset


# ---------- Фильтр над списком ----------


class StatusPeriodFilter(admin.ListFilter):
    """Статус кнопками и «Период ▾» — полосой над списком, а не в колонке фильтров.

    Параметры адреса — `status__exact` (как у прежнего фильтра «статус»: старые
    ссылки и наборы «Моих фильтров» работают), `period` — готовый период и
    `period_from`, `period_to` — свой. Колонка фильтров его не рисует (пустой
    шаблон); полосу рисует тег `status_bar`.
    """

    title = "статус"
    template = "admin/seo_hidden_filter.html"
    status_param = "status__exact"
    period_param = "period"
    from_param = "period_from"
    to_param = "period_to"
    statuses: tuple[WorkStatus, ...] = ()

    def __init__(
        self, request: HttpRequest, params: dict[str, Any], model: Any, model_admin: Any
    ) -> None:
        super().__init__(request, params, model, model_admin)
        self.used_parameters: dict[str, object] = {}
        for name in self.expected_parameters():
            if name is not None and name in params:
                value = params.pop(name)
                self.used_parameters[name] = value[-1] if isinstance(value, list) else value
        self.request = request

    @property
    def status(self) -> str | None:
        value = str(self.used_parameters.get(self.status_param) or "")
        return value if value in {str(s) for s in self.statuses} else None

    @property
    def period(self) -> Period:
        used = {name: str(value) for name, value in self.used_parameters.items()}
        return parse_period(
            used.get(self.period_param), used.get(self.from_param), used.get(self.to_param)
        )

    def has_output(self) -> bool:
        return True

    def expected_parameters(self) -> list[str | None]:
        return [self.status_param, self.period_param, self.from_param, self.to_param]

    def choices(self, changelist: Any) -> Iterator[Any]:
        # В колонке фильтров ничего: полосу над списком рисует `status_bar`.
        return iter(())

    def status_choices(self, changelist: Any) -> list[dict[str, Any]]:
        """Кнопки статусов: «Все» и статусы списка, со ссылками на тот же список."""
        current = self.status
        rows = [
            {
                "label": "Все",
                "selected": current is None,
                "url": changelist.get_query_string(remove=[self.status_param]),
            }
        ]
        rows += [
            {
                "label": status.label,
                "selected": current == status,
                "url": changelist.get_query_string({self.status_param: status}),
            }
            for status in self.statuses
        ]
        return rows

    def period_choices(self, changelist: Any) -> list[dict[str, Any]]:
        current = self.period.key
        own = [self.from_param, self.to_param]
        rows = [
            {
                "label": label,
                "selected": current == key,
                "url": changelist.get_query_string({self.period_param: key}, remove=own),
            }
            for key, label, _ in PRESETS
        ]
        rows.append(
            {
                "label": ALL_TIME,
                "selected": not current,
                "url": changelist.get_query_string(remove=[self.period_param, *own]),
            }
        )
        return rows

    def kept_params(self, changelist: Any) -> list[tuple[str, str]]:
        """Параметры адреса для формы «свой период»: всё, кроме самого периода и страницы."""
        skip = {self.period_param, self.from_param, self.to_param}
        # filter_params — все значения параметра (у фильтров с выбором нескольких),
        # без номера страницы: после смены периода список — с первой страницы.
        return [
            (name, str(value))
            for name, values in changelist.filter_params.items()
            for value in (values if isinstance(values, list) else [values])
            if name not in skip
        ]


class SiteStatusFilter(StatusPeriodFilter):
    """«Площадки»: решение или статусы размещений рабочего продукта."""

    statuses = (
        WorkStatus.NEW,
        WorkStatus.VIEWED,
        WorkStatus.IN_WORK,
        WorkStatus.ORDERED,
        WorkStatus.WRITING,
        WorkStatus.PLACED,
        WorkStatus.DISCARDED,
        WorkStatus.REJECTED,
        WorkStatus.BLACKLISTED,
    )

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        return filter_sites(queryset, self.status, self.period)


class PlacementStatusFilter(StatusPeriodFilter):
    """«Размещения»: статус размещения, поставленный в период.

    Кнопки — статусы, какие бывают у размещения: «Отбрасываю» и «Чёрный
    список» — решения по площадке, у размещения их не ставят.
    """

    statuses = (
        WorkStatus.IN_WORK,
        WorkStatus.ORDERED,
        WorkStatus.WRITING,
        WorkStatus.PLACED,
        WorkStatus.REJECTED,
    )

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        return filter_placements(queryset, self.status, self.period)


def period_of(changelist: Any) -> Period:
    """Период списка — для фишек: какие закрытые размещения показывать."""
    for spec in getattr(changelist, "filter_specs", ()):
        if isinstance(spec, StatusPeriodFilter):
            return spec.period
    return Period()


# ---------- Фишки ----------


@dataclass(frozen=True)
class Chip:
    """Фишка статуса: «Заявка отправлена 09.10.26», группа — «Размещено ×8»."""

    label: str
    tone: str  # live — идёт, ok — размещено, no — закрыто без статьи
    url: str  # полная страница; в панели — data-panel
    panel: str
    title: str
    when: datetime | None = None
    count: int = 1


@dataclass
class Cell:
    """Статус площадки у продукта: фишки размещений и «больше не работаем».

    Размещений нет — фишек нет, статус показывают по решению по площадке, а
    `decided` — когда его поставили.
    """

    chips: list[Chip] = field(default_factory=list)
    # Размещения есть, но все закрыты раньше периода: ссылка на текущее.
    hidden_url: str = ""
    placements: int = 0
    decided: datetime | None = None


def cells(site_ids: Iterable[int], product_id: int | None, period: Period) -> dict[int, Cell]:
    """Колонка «статус» строк страницы: площадка → фишки размещений продукта за период.

    Два запроса на страницу: размещения с датами статусов и решения по
    площадкам с датами.
    """
    wanted = sorted(set(site_ids))
    if not wanted or product_id is None:
        return {}
    found = _cells(
        Placement.objects.filter(site_id__in=wanted, product_id=product_id),
        ProductSite.objects.filter(site_id__in=wanted, product_id=product_id),
        period,
    )
    return {site_id: cell for (site_id, _), cell in found.items()}


def product_cells(site_id: int) -> dict[int, Cell]:
    """Карточка площадки: продукт → фишки его размещений за всё время."""
    found = _cells(
        Placement.objects.filter(site_id=site_id),
        ProductSite.objects.filter(site_id=site_id),
        Period(),
    )
    return {product_id: cell for (_, product_id), cell in found.items()}


def _cells(
    placements: QuerySet[Placement], decisions: QuerySet[ProductSite], period: Period
) -> dict[tuple[int, int], Cell]:
    """Ячейки пар «площадка × продукт»: размещения и решение по площадке."""
    items = list(
        placements.select_related("seller")
        .annotate(status_at=repeat.status_at())
        .order_by("site_id", "product_id", "pk")
    )
    dates: dict[int, datetime] = {item.pk: item.status_at for item in items}
    groups: dict[tuple[int, int], list[Placement]] = {}
    for item in items:
        groups.setdefault((item.site_id, item.product_id), []).append(item)
    result: dict[tuple[int, int], Cell] = {}
    rows = decisions.annotate(decided_at=decision_at()).values_list(
        "pk", "site_id", "product_id", "status", "decided_at"
    )
    for row_id, site_id, product_id, status, decided_at in rows:
        group = groups.pop((site_id, product_id), [])
        cell = _cell(group, dates, period)
        cell.decided = decided_at
        # «Больше не работаем» — после размещений и при любом периоде.
        if group and status in STOPS:
            cell.chips.append(stop_chip(row_id, status, decided_at))
        result[(site_id, product_id)] = _finish(cell, group)
    # Размещение без строки решения (её заводит система) — фишки всё равно видны.
    for key, group in groups.items():
        result[key] = _finish(_cell(group, dates, period), group)
    return result


def _cell(items: list[Placement], dates: dict[int, datetime], period: Period) -> Cell:
    """Размещения продукта на площадке: закрытые за период, за ними идущие.

    Номер — как у вкладок: по порядку заведения. Фишек за период больше трёх —
    закрытые с одним статусом собираются в одну («Размещено ×8»).
    """
    numbers = {item.pk: number for number, item in enumerate(items, start=1)}
    shown = [p for p in items if p.status in repeat.ACTIVE or period.holds(dates[p.pk])]
    closed = [p for p in shown if p.status not in repeat.ACTIVE]
    live = [p for p in shown if p.status in repeat.ACTIVE]
    cell = Cell(placements=len(items))
    if len(shown) > MANY:
        by_status: dict[str, list[Placement]] = {}
        for item in closed:
            by_status.setdefault(item.status, []).append(item)
        for status, group in by_status.items():
            if len(group) == 1:
                cell.chips.append(_chip(group[0], numbers, dates))
            else:
                cell.chips.append(_group(status, group, numbers, dates))
    else:
        cell.chips += [_chip(item, numbers, dates) for item in closed]
    cell.chips += [_chip(item, numbers, dates) for item in live]
    return cell


def _finish(cell: Cell, group: list[Placement]) -> Cell:
    """Размещения есть, а фишек за период нет — ссылка «нет за период» на текущее."""
    if group and not cell.chips:
        now = repeat.current(group)
        cell.hidden_url = _url(now) if now is not None else ""
    return cell


def stop_chip(row_id: int, status: str, when: datetime | None) -> Chip:
    """«Больше не работаем» при размещениях — решение по площадке, красной фишкой."""
    return Chip(
        label=str(WorkStatus(status).label),
        tone="no",
        url=reverse("admin:sites_productsite_change", args=[row_id]),
        panel=reverse("admin:sites_productsite_decision", args=[row_id]),
        title="Больше не работаем — решение по площадке",
        when=when,
    )


def _url(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _chip(placement: Placement, numbers: dict[int, int], dates: dict[int, datetime]) -> Chip:
    seller = placement.seller.name if placement.seller is not None else "без продавца"
    url = _url(placement)
    return Chip(
        label=placement.get_status_display(),
        tone=repeat.tone(placement.status),
        url=url,
        panel=url,
        title=f"№{numbers[placement.pk]} · {seller}",
        when=dates.get(placement.pk),
    )


def _group(
    status: str, group: list[Placement], numbers: dict[int, int], dates: dict[int, datetime]
) -> Chip:
    """Несколько закрытых с одним статусом — одна фишка; открывает последнее,
    остальные — вкладками в панели. В подсказке — номера и даты всех."""
    last = group[-1]
    url = _url(last)
    parts = [
        f"№{numbers[item.pk]} {timezone.localtime(dates[item.pk]):%d.%m.%y}"
        for item in group
        if dates.get(item.pk) is not None
    ]
    return Chip(
        label=last.get_status_display(),
        tone=repeat.tone(status),
        url=url,
        panel=url,
        title=" · ".join(parts),
        count=len(group),
    )


# ---------- Разметка ----------


def chip_html(chip: Chip) -> SafeString:
    """Фишка ссылкой: открывает своё размещение (или решение) панелью."""
    count = f" ×{chip.count}" if chip.count > 1 else ""
    when = (
        format_html(
            '<span class="seo-st-when">{}</span>', f"{timezone.localtime(chip.when):%d.%m.%y}"
        )
        if chip.when is not None
        else ""
    )
    return format_html(
        '<a class="seo-st seo-st-{}" href="{}" data-panel="{}" title="{}">{}{}{}</a>',
        chip.tone,
        chip.url,
        chip.panel,
        chip.title,
        chip.label,
        count,
        when,
    )


def cell_html(cell: Cell) -> SafeString:
    """Фишки размещений и «больше не работаем»; за период нет ни одной —
    «нет за период» ссылкой на текущее размещение."""
    if cell.chips:
        return format_html(
            '<span class="seo-st-chips">{}</span>',
            format_html_join("", "{}", ((chip_html(chip),) for chip in cell.chips)),
        )
    return format_html(
        '<a class="seo-st-none" href="{}" data-panel title="{}">нет за период</a>',
        cell.hidden_url,
        "Размещения есть, но раньше выбранного периода",
    )


def decided_html(when: datetime | None) -> SafeString | str:
    """Дата решения по площадке рядом со статусом: «12.10.26»."""
    if when is None:
        return ""
    return format_html(
        '<span class="seo-decided">{}</span>', f"{timezone.localtime(when):%d.%m.%y}"
    )
