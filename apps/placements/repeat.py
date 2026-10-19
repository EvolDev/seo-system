"""Повторные размещения продукта на одной площадке (ADR-073).

Размещение — одна сделка: своя цена, счёт, даты, ссылки и проверки. У продукта
на площадке их бывает несколько — одно за другим или параллельно. Здесь то,
что для этого нужно экранам:

- вкладки панели «Площадка · №1 · №2 · + Ещё размещение»: размещения продукта
  на площадке в порядке заведения, номер — порядковый;
- текущее размещение — идущее («В работе», «Заявка отправлена», «Написание
  статьи»), иначе последнее: его открывает щелчок по площадке;
- когда поставлен нынешний статус — из истории статусов (ADR-049);
- «Взять в размещение» для отмеченных площадок: тем, где размещение уже есть,
  ещё одно — только явно;
- предложение размещения — его продавец и цена, зафиксированные при заведении:
  смена рабочей цены площадки размещения не трогает, продавца меняют только
  в карточке размещения.

Статус площадки у продукта ведётся как прежде (`apps.sites.statuses`): на нём
стоят отчёты, импорт таблицы и фильтры.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db.models import (
    Case,
    DateTimeField,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.urls import reverse

from apps.placements.models import STATUS_STAMPS, Placement, PlacementStatusChange
from apps.sites import statuses
from apps.sites.models import (
    ProductSite,
    Site,
    SiteOffer,
    SitePrice,
    StatusSource,
    WorkStatus,
)
from config.changes import stamped

# Сделка ещё идёт: по ней ждут заявку, статью или публикацию.
ACTIVE = frozenset({WorkStatus.IN_WORK, WorkStatus.ORDERED, WorkStatus.WRITING})
# Больше вкладок размещений — ранние уходят в «Ещё N ▾»; видны последние,
# идущие и открытое.
SHOWN_TABS = 4
LAST_TABS = 2


@dataclass(frozen=True)
class Tab:
    """Вкладка размещения: «№2 · Collaborator», под ним статус и дата статуса."""

    pk: int
    number: int
    seller: str
    status: str
    tone: str
    when: datetime | None
    url: str
    delete_url: str | None
    selected: bool


@dataclass(frozen=True)
class Tabs:
    """Шапка панели площадки: «Площадка», размещения продукта и «+ Ещё размещение»."""

    site_url: str
    site_selected: bool
    site_sub: str
    shown: list[Tab]
    hidden: list[Tab]
    add_url: str | None
    add_label: str
    add_selected: bool
    # Почему нельзя завести размещение — подсказка на кнопке, пусто — можно.
    blocked: str = ""

    @property
    def hidden_range(self) -> str:
        """«№1–№8» под «Ещё 8 ▾»."""
        if not self.hidden:
            return ""
        first, last = self.hidden[0].number, self.hidden[-1].number
        return f"№{first}" if first == last else f"№{first}–№{last}"


def of_site(site_id: int, product_id: int) -> list[Placement]:
    """Размещения продукта на площадке в порядке заведения: №1 — первое."""
    return list(
        Placement.objects.filter(site_id=site_id, product_id=product_id)
        .select_related("seller")
        .order_by("pk")
    )


def current(placements: Sequence[Placement]) -> Placement | None:
    """Идущее размещение (последнее из идущих), иначе последнее; нет — None."""
    for placement in reversed(placements):
        if placement.status in ACTIVE:
            return placement
    return placements[-1] if placements else None


def one_per_site(queryset: QuerySet[Placement]) -> QuerySet[Placement]:
    """Одна строка на площадку и продукт: из размещений `queryset` — текущее.

    Текущее — идущее (последнее из идущих), иначе последнее заведённое. Порядок,
    фильтры и пометки самого `queryset` остаются: отбор — подзапросом по номерам.
    """
    best = (
        queryset.order_by()
        .annotate(idle=Case(When(status__in=ACTIVE, then=Value(0)), default=Value(1)))
        .order_by("site_id", "product_id", "idle", "-pk")
        .distinct("site_id", "product_id")
        .values("pk")
    )
    return queryset.filter(pk__in=Subquery(best))


def offer_id() -> Coalesce:
    """Предложение размещения — выражение для запроса: номер снимка цены.

    Запомненное — через что взяли (`price`, ADR-073). У размещений до ADR-073 —
    снимок цены их продавца на ту же услугу, действовавший, когда размещение
    завели (не было — первый после). Нет и его — пусто: цен в строке нет, в
    плане — без суммы, пока не впишут «Заплачено»; цену чужого продавца не
    подставляем. Продавца у размещения нет вовсе — рабочая цена площадки:
    закреплять нечего. Смена рабочей цены площадки предложение размещения с
    продавцом не меняет. Та же логика в SQL «Экономики» (`economics._ROWS_SQL`,
    CTE `own`).
    """
    before = Q(checked_at__lte=OuterRef("created_at"))
    own = (
        SitePrice.objects.filter(
            site_id=OuterRef("site_id"),
            seller_id=OuterRef("seller_id"),
            placement_type=OuterRef("placement_type"),
            withdrawn=False,
        )
        .annotate(
            late=Case(When(before, then=Value(0)), default=Value(1)),
            before_at=Case(When(before, then=F("checked_at"))),
            before_pk=Case(When(before, then=F("pk"))),
        )
        .order_by(
            "late",
            F("before_at").desc(nulls_last=True),
            F("before_pk").desc(nulls_last=True),
            "checked_at",
            "pk",
        )
        .values("pk")[:1]
    )
    unassigned = Case(When(seller_id__isnull=True, then=F("site__price_id")))
    return Coalesce(F("price_id"), Subquery(own), unassigned)


def offers_of(placements: Iterable[Placement]) -> dict[int, SitePrice]:
    """Предложения размещений (`offer_id`) с продавцами — двумя запросами."""
    ids = [item.pk for item in placements]
    if not ids:
        return {}
    picked = dict(
        Placement.objects.filter(pk__in=ids)
        .annotate(offer=offer_id())
        .exclude(offer=None)
        .values_list("pk", "offer")
    )
    prices = SitePrice.objects.select_related("seller").in_bulk(set(picked.values()))
    return {pk: prices[offer] for pk, offer in picked.items() if offer in prices}


def current_offer(
    site_id: int, seller_id: int | None, placement_type: str | None
) -> SitePrice | None:
    """Текущее предложение продавца на услугу (`v_site_offers`) — когда продавца
    размещения меняют руками. Снял площадку с продажи или цены нет — None."""
    if seller_id is None or not placement_type:
        return None
    found = (
        SiteOffer.objects.filter(
            site_id=site_id, seller_id=seller_id, placement_type=placement_type
        )
        .values_list("pk", flat=True)
        .first()
    )
    return SitePrice.objects.filter(pk=found).first() if found is not None else None


def status_at() -> Coalesce:
    """Когда поставлен нынешний статус размещения — выражение для запроса.

    Последняя смена на этот статус в истории (ADR-049, копится с 03.10.2026);
    её нет — дата заявки у «Заявки отправлена», публикации у «Размещено», иначе
    когда размещение завели. Одно выражение на вкладки, фишки и отбор по периоду.
    """
    changed = (
        PlacementStatusChange.objects.filter(
            placement_id=OuterRef("pk"), to_status=OuterRef("status")
        )
        .order_by("-changed_at", "-pk")
        .values("changed_at")[:1]
    )
    stamp = Case(
        *(When(status=status, then=F(name)) for status, name in STATUS_STAMPS.items()),
        default=None,
        output_field=DateTimeField(),
    )
    return Coalesce(Subquery(changed), stamp, F("created_at"), output_field=DateTimeField())


def status_dates(placements: Iterable[Placement]) -> dict[int, datetime]:
    """Когда поставлен нынешний статус каждого размещения (`status_at`), одним запросом."""
    ids = [item.pk for item in placements]
    if not ids:
        return {}
    rows = Placement.objects.filter(pk__in=ids).annotate(at=status_at()).values_list("pk", "at")
    return dict(rows)


# Порядок статусов сделки: «дальше» — ближе к публикации.
LADDER = (WorkStatus.IN_WORK, WorkStatus.ORDERED, WorkStatus.WRITING, WorkStatus.PLACED)


def implied_status(placements: Sequence[Placement]) -> WorkStatus:
    """Статус площадки по её размещениям: идущее, что дальше всех, иначе
    «Размещено», если статья есть, иначе «Просмотрено». Для решения «работаем»."""
    live = [placement.status for placement in placements if placement.status in ACTIVE]
    if live:
        return WorkStatus(max(live, key=LADDER.index))
    if any(placement.status == WorkStatus.PLACED for placement in placements):
        return WorkStatus.PLACED
    return WorkStatus.VIEWED


def tone(status: str) -> str:
    """Цвет вкладки и фишки: идёт, размещено или закрыто без статьи."""
    if status in ACTIVE:
        return "live"
    return "ok" if status == WorkStatus.PLACED else "no"


def blocked_reason(site_id: int, product_id: int) -> str:
    """Почему нельзя завести размещение: чёрный список у продукта. Можно — пусто."""
    row = (
        ProductSite.objects.filter(site_id=site_id, product_id=product_id)
        .select_related("product")
        .first()
    )
    if row is not None and row.status == WorkStatus.BLACKLISTED:
        return f"Площадка в чёрном списке у {row.product.name}: сначала снимите его."
    return ""


def tabs(
    site_id: int,
    product_id: int,
    selected: int | str,
    *,
    can_add: bool,
    can_delete: bool,
    site_sub: str = "",
) -> Tabs:
    """Вкладки панели; `selected` — номер размещения, «site» или «new»."""
    placements = of_site(site_id, product_id)
    dates = status_dates(placements)
    items = [
        Tab(
            pk=placement.pk,
            number=number,
            seller=placement.seller.name if placement.seller is not None else "без продавца",
            status=placement.get_status_display(),
            tone=tone(placement.status),
            when=dates.get(placement.pk),
            url=reverse("admin:placements_placement_change", args=[placement.pk]),
            delete_url=reverse("admin:placements_placement_delete", args=[placement.pk])
            if can_delete
            else None,
            selected=selected == placement.pk,
        )
        for number, placement in enumerate(placements, start=1)
    ]
    shown, hidden = items, []
    if len(items) > SHOWN_TABS:
        keep = {tab.pk for tab in items[-LAST_TABS:]}
        keep |= {p.pk for p in placements if p.status in ACTIVE}
        keep |= {tab.pk for tab in items if tab.selected}
        shown = [tab for tab in items if tab.pk in keep]
        hidden = [tab for tab in items if tab.pk not in keep]
    blocked = blocked_reason(site_id, product_id)
    add_url = None
    if can_add and not blocked:
        add_url = f"{reverse('admin:placements_placement_add')}?site={site_id}&product={product_id}"
    if not site_sub:
        count = len(items)
        site_sub = _count(count) if count else "размещений нет"
    return Tabs(
        site_url=reverse("admin:sites_site_card", args=[site_id]),
        site_selected=selected == "site",
        site_sub=site_sub,
        shown=shown,
        hidden=hidden,
        add_url=add_url,
        add_label="+ Ещё размещение" if items else "+ Взять в размещение",
        add_selected=selected == "new",
        blocked=blocked,
    )


@dataclass
class Taken:
    """Итог «Взять в размещение» по отмеченным площадкам."""

    created: list[Placement] = field(default_factory=list)
    again: list[Placement] = field(default_factory=list)
    skipped: int = 0
    blacklisted: int = 0


@dataclass(frozen=True)
class Candidate:
    """Отмеченная площадка в окне «Взять в размещение»: что с ней сделаем."""

    site_id: int
    domain: str
    current: Placement | None
    current_when: datetime | None
    count: int
    blacklisted: bool


def candidates(site_ids: Iterable[int], product_id: int) -> list[Candidate]:
    """Площадки окна: без размещений, с размещениями (ещё одно — галочкой), в чёрном списке."""
    ids = list(dict.fromkeys(site_ids))
    domains = dict(Site.all_objects.filter(pk__in=ids).values_list("pk", "domain"))
    black = set(
        ProductSite.objects.filter(
            site_id__in=ids, product_id=product_id, status=WorkStatus.BLACKLISTED
        ).values_list("site_id", flat=True)
    )
    by_site: dict[int, list[Placement]] = {}
    for placement in (
        Placement.objects.filter(site_id__in=ids, product_id=product_id)
        .select_related("seller")
        .order_by("pk")
    ):
        by_site.setdefault(placement.site_id, []).append(placement)
    shown = [now for placements in by_site.values() if (now := current(placements)) is not None]
    dates = status_dates(shown)
    result = []
    for site_id in ids:
        if site_id not in domains:
            continue
        placements = by_site.get(site_id, [])
        now = current(placements)
        result.append(
            Candidate(
                site_id=site_id,
                domain=domains[site_id],
                current=now,
                current_when=dates.get(now.pk) if now is not None else None,
                count=len(placements),
                blacklisted=site_id in black,
            )
        )
    return result


def take(
    site_ids: Iterable[int],
    product_id: int,
    *,
    again: Iterable[int] = (),
    employee: Any = None,
) -> Taken:
    """Заводит размещения продукта отмеченным площадкам — «В работе», от рабочей цены.

    Где размещение уже есть, ещё одно — только для площадок из `again`: может
    идти параллельно с идущим. Чёрный список пропускается всегда. Продавец,
    услуга и предложение — из рабочей цены площадки, сотрудник — `employee`;
    статус площадки двигает сам `Placement.save()` (ADR-062).
    """
    wanted = set(again)
    result = Taken()
    with stamped(source=StatusSource.FORM):
        for candidate in candidates(site_ids, product_id):
            if candidate.blacklisted:
                result.blacklisted += 1
                continue
            if candidate.count and candidate.site_id not in wanted:
                result.skipped += 1
                continue
            site = Site.all_objects.select_related("price").get(pk=candidate.site_id)
            placement = create(site, product_id, site.price, employee=employee)
            (result.again if candidate.count else result.created).append(placement)
    return result


def create(
    site: Site, product_id: int, price: SitePrice | None, *, employee: Any = None
) -> Placement:
    """Новое размещение «В работе» через предложение `price`: продавец, услуга, тип ссылки.

    Заводит человек — «Отбрасываю» и «Отказ» площадки снимаются (`statuses.reopen`).
    """
    placement = Placement.objects.create(
        site=site,
        product_id=product_id,
        seller_id=price.seller_id if price is not None else None,
        placement_type=price.placement_type if price is not None else None,
        link_type=price.link_type if price is not None else None,
        price=price,
        employee=employee,
    )
    statuses.reopen(site.pk, product_id, placement_id=placement.pk)
    return placement


def _count(count: int) -> str:
    """«1 размещение», «2 размещения», «5 размещений»."""
    tail = count % 100
    if 11 <= tail <= 14:
        word = "размещений"
    elif count % 10 == 1:
        word = "размещение"
    elif 2 <= count % 10 <= 4:
        word = "размещения"
    else:
        word = "размещений"
    return f"{count} {word}"
