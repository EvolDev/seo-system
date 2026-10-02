"""План загрузки: куда попадёт каждое предложение из файла — без записи в базу.

Одна функция и для сводки до записи, и для самой записи: запись строит план
заново внутри своей транзакции, по свежему состоянию базы (ADR-044).

Правило разбора (ADR-043, уточнено ADR-044):
- площадки нет в базе или у неё нет рабочей цены — рабочей становится
  первая цена сама: публикация, если она есть в строке, иначе вставка;
- тот же продавец за ту же услугу — рабочая цена сама переходит на новую,
  на сколько бы та ни изменилась: цены в евро плывут с курсом. Пока по
  площадке заявка в работе, рабочая не двигается — решает человек;
- другой продавец или другая услуга — ждёт решения. Не ждёт, если этот
  продавец уже присылал цену, её разобрали, и она изменилась не больше
  порога `OFFER_RECHECK`;
- площадку отклоняли («Отбрасываю», «Отказала площадка», чёрный список) —
  то, что ждёт решения, попадает во вкладку «Отклоняли»: сначала посмотреть
  причину.
"""

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q
from django.utils import timezone

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    PlacementType,
    ProductSite,
    ReviewGroup,
    Site,
    SitePrice,
    SiteStatus,
)
from apps.sites.rates import to_eur_cents
from apps.sites.uploads.records import Parsed, Record

# Заявка в работе — цена договорённая, рабочая сама не меняется.
ORDER_IN_WORK = (PlacementStatus.ORDERED, PlacementStatus.WRITING, PlacementStatus.REVIEW)
# Вкладка «Отклоняли»: отказались мы, отказала площадка или чёрный список (ADR-047).
REJECTED_STATUSES = (SiteStatus.DISCARDED, SiteStatus.DECLINED, SiteStatus.BLACKLISTED)
# Длинные списки сводки обрезаются: каталог — 45 000 строк.
LIST_LIMIT = 300
# Запросы с доменами и id — пачками, чтобы не собирать один запрос на 45 000 значений.
CHUNK = 5000


def refusal_text(row: ProductSite) -> str:
    """Отказ по продукту одной строкой: «Convertio: Отбрасываю — Nofollow»."""
    text = f"{row.product.name}: {row.get_status_display()}"
    return f"{text} — {row.reject_reason}" if row.reject_reason else text


@dataclass(frozen=True)
class OfferState:
    id: int
    seller_id: int
    service: str
    cents: int | None
    currency: str
    reviewed: bool
    eur_cents: int | None


@dataclass
class SiteState:
    id: int
    domain: str
    working: OfferState | None = None
    rejected: list[str] = field(default_factory=list)  # «Convertio: Отбрасываю — Nofollow»
    frozen: bool = False
    prev: dict[str, OfferState] = field(default_factory=dict)  # этого продавца, до даты цен


@dataclass(frozen=True)
class Decision:
    group: ReviewGroup
    needs_decision: bool
    becomes_working: bool


@dataclass
class PlanItem:
    """Одно предложение из файла: площадка, услуга, цена и решение по нему."""

    record: Record
    service: PlacementType
    cents: int
    eur_cents: int | None
    site: SiteState | None  # None — площадки ещё нет в базе
    decision: Decision

    @property
    def diff_eur_cents(self) -> int | None:
        """Разница с рабочей ценой в евроцентах: минус — дешевле."""
        working = self.site.working if self.site else None
        if working is None or working.eur_cents is None or self.eur_cents is None:
            return None
        return self.eur_cents - working.eur_cents


@dataclass
class Plan:
    seller_id: int
    currency: str
    parsed: Parsed
    items: list[PlanItem]
    sites: dict[str, SiteState]
    # Удалённые из базы: в загрузке они снова новые, запись снимет пометку.
    restore: dict[str, int] = field(default_factory=dict)
    # Заблокированные (чёрный список у всех продуктов): загрузка их пропускает.
    blocked: list[str] = field(default_factory=list)

    def select(self, keep: "Callable[[PlanItem], bool]") -> "Plan":
        """План только из нужных строк — для «Обновить в базе» и «Добавить новые»."""
        return replace(self, items=[item for item in self.items if keep(item)])

    @property
    def known(self) -> int:
        return len({item.record.domain for item in self.items if item.site is not None})

    def summary(self, *, rows: int, blank_rows: int, unknown: Sequence[str] = ()) -> dict[str, Any]:
        """Сводка до записи — то, что видит человек перед «Записать».

        `known_*` — то же только по площадкам, которые уже есть в базе: их
        у каталога обновляет отдельная кнопка «Обновить в базе».
        """
        groups = {group.value: 0 for group in ReviewGroup}
        known_groups = {group.value: 0 for group in ReviewGroup}
        down = up = 0
        for item in self.items:
            groups[item.decision.group.value] += 1
            if item.site is not None:
                known_groups[item.decision.group.value] += 1
            if item.decision.group == ReviewGroup.CHANGED and item.site and item.site.working:
                diff = item.diff_eur_cents
                if diff is not None and diff < 0:
                    down += 1
                elif diff is not None and diff > 0:
                    up += 1
        records = self.parsed.records
        rejected = []
        for record in records:
            site = self.sites.get(record.domain)
            if site is not None and site.rejected:
                rejected.append({"domain": record.domain, "reasons": site.rejected})
        errors = [{"line": e.line, "message": e.message} for e in self.parsed.errors]
        urls = [
            {"line": r.line, "value": r.source_value, "domain": r.domain}
            for r in records
            if r.source_value
        ]
        duplicates = [
            {"domain": d.domain, "lines": list(d.lines), "prices": list(d.prices)}
            for d in self.parsed.duplicates
        ]
        blocked = set(self.blocked)
        planned = [r for r in records if r.domain not in blocked]
        return {
            "rows": rows,
            "blank_rows": blank_rows,
            "sites": len(planned),
            "known": self.known,
            "new": len(planned) - self.known,
            "offers": len(self.items),
            "groups": groups,
            "known_groups": known_groups,
            "needs_decision": sum(item.decision.needs_decision for item in self.items),
            "known_needs_decision": sum(
                item.decision.needs_decision for item in self.items if item.site is not None
            ),
            "changed_down": down,
            "changed_up": up,
            "currency": self.currency,
            **_capped("rejected", sorted(rejected, key=lambda r: str(r["domain"]))),
            **_capped("duplicates", duplicates),
            **_capped("errors", sorted(errors, key=lambda e: int(str(e["line"])))),
            **_capped("urls", urls),
            **_capped("blocked", [{"domain": domain} for domain in sorted(blocked)]),
            "unknown": list(unknown),
        }


def classify(
    *,
    service: str,
    cents: int,
    currency: str,
    eur_cents: int | None,
    seller_id: int,
    site: SiteState | None,
    record_has_gp: bool,
    recheck_pct: float,
) -> Decision:
    """Куда попадёт предложение и нужно ли решение человека. Правило — в начале модуля."""
    first_service = service == PlacementType.GUEST_POST or not record_has_gp
    working = site.working if site is not None else None
    if working is None:
        rejected = site is not None and bool(site.rejected)
        group = ReviewGroup.REJECTED if rejected else ReviewGroup.NEW
        return Decision(group, needs_decision=False, becomes_working=first_service)

    frozen = site is not None and site.frozen
    same_price = working.cents == cents and working.currency == currency
    if working.seller_id == seller_id and working.service == service:
        if same_price:
            # Та же цена — рабочая переходит на свежий снимок: дата цены актуальная.
            return Decision(ReviewGroup.SAME, needs_decision=False, becomes_working=not frozen)
        return Decision(ReviewGroup.CHANGED, needs_decision=frozen, becomes_working=not frozen)

    if working.service != service:
        group = ReviewGroup.OTHER_SERVICE
    elif eur_cents is None or working.eur_cents is None or eur_cents == working.eur_cents:
        group = ReviewGroup.SAME
    elif eur_cents < working.eur_cents:
        group = ReviewGroup.CHEAPER
    else:
        group = ReviewGroup.PRICIER
    needs = group != ReviewGroup.SAME
    prev = site.prev.get(service) if site is not None else None
    if needs and prev is not None and prev.reviewed and _within(prev, cents, currency, recheck_pct):
        needs = False
    if needs and site is not None and site.rejected:
        group = ReviewGroup.REJECTED
    return Decision(group, needs_decision=needs, becomes_working=False)


def build_plan(
    parsed: Parsed,
    *,
    seller_id: int,
    currency: str,
    prices_date: dt.date,
    rates: dict[str, Decimal],
    recheck_pct: float,
) -> Plan:
    domains = [record.domain for record in parsed.records]
    state = load_state(domains, seller_id=seller_id, before=start_of_day(prices_date), rates=rates)
    items: list[PlanItem] = []
    for record in parsed.records:
        if record.domain in state.blocked:
            continue
        site = state.sites.get(record.domain)
        has_gp = PlacementType.GUEST_POST in record.offers
        # Публикация — первой: при новой площадке рабочей станет она.
        for service in sorted(record.offers, key=lambda s: s != PlacementType.GUEST_POST):
            cents = record.offers[service]
            eur = to_eur_cents(cents, currency, rates)
            decision = classify(
                service=service,
                cents=cents,
                currency=currency,
                eur_cents=eur,
                seller_id=seller_id,
                site=site,
                record_has_gp=has_gp,
                recheck_pct=recheck_pct,
            )
            items.append(PlanItem(record, PlacementType(service), cents, eur, site, decision))
    return Plan(
        seller_id, currency, parsed, items, state.sites, state.deleted, sorted(state.blocked)
    )


def start_of_day(day: dt.date) -> dt.datetime:
    """Дата цен → момент для базы: начало дня по времени проекта, как у импорта таблицы."""
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


@dataclass
class BaseState:
    sites: dict[str, SiteState]
    deleted: dict[str, int]  # домен → id удалённой площадки
    blocked: set[str]


def blocked_ids(site_ids: Iterable[int]) -> set[int]:
    """Площадки в чёрном списке у всех активных продуктов — «заблокированные» (ADR-044)."""
    result: set[int] = set()
    for chunk in _chunks(list(site_ids)):
        rows = (
            ProductSite.objects.filter(site_id__in=chunk, product__is_active=True)
            .values("site_id")
            .annotate(total=Count("pk"), black=Count("pk", filter=Q(status=SiteStatus.BLACKLISTED)))
        )
        result.update(
            row["site_id"] for row in rows if row["total"] and row["total"] == row["black"]
        )
    return result


def load_state(
    domains: Sequence[str], *, seller_id: int, before: dt.datetime, rates: dict[str, Decimal]
) -> BaseState:
    """Что база знает о площадках файла: рабочая цена, отказы, заявки, прежние цены продавца.

    Удалённая площадка для загрузки — новая (её снова можно добавить),
    заблокированная — пропускается целиком.
    """
    sites: dict[str, SiteState] = {}
    deleted: dict[str, int] = {}
    price_ids: dict[int, int] = {}
    for domain_chunk in _chunks(domains):
        rows = Site.all_objects.filter(domain__in=domain_chunk).values_list(
            "id", "domain", "is_deleted", "price_id"
        )
        for site_id, domain, is_deleted, price_id in rows:
            if is_deleted:
                deleted[domain] = site_id
                continue
            sites[domain] = SiteState(site_id, domain)
            if price_id is not None:
                price_ids[site_id] = price_id
    blocked_site_ids = blocked_ids(state.id for state in sites.values())
    blocked = {domain for domain, state in sites.items() if state.id in blocked_site_ids}
    for domain in blocked:
        del sites[domain]
    by_id = {state.id: state for state in sites.values()}
    price_ids = {site_id: price for site_id, price in price_ids.items() if site_id in by_id}
    ids = list(by_id)

    for price_chunk in _chunks(list(price_ids.values())):
        for offer in SitePrice.objects.filter(pk__in=price_chunk):
            by_id[offer.site_id].working = _state(offer, rates)

    for chunk in _chunks(ids):
        rejected = (
            ProductSite.objects.filter(
                site_id__in=chunk, status__in=REJECTED_STATUSES, product__is_active=True
            )
            .select_related("product")
            .order_by("product__name")
        )
        for row in rejected:
            by_id[row.site_id].rejected.append(refusal_text(row))
        frozen = Placement.objects.filter(site_id__in=chunk, status__in=ORDER_IN_WORK)
        for site_id in frozen.values_list("site_id", flat=True).distinct():
            by_id[site_id].frozen = True
        # DISTINCT ON (площадка, услуга) — последняя цена этого продавца до даты загрузки.
        previous = (
            SitePrice.objects.filter(site_id__in=chunk, seller_id=seller_id, checked_at__lt=before)
            .order_by("site_id", "placement_type", "-checked_at", "-pk")
            .distinct("site_id", "placement_type")
        )
        for offer in previous:
            by_id[offer.site_id].prev[offer.placement_type] = _state(offer, rates)
    return BaseState(sites, deleted, blocked)


def _state(offer: SitePrice, rates: dict[str, Decimal]) -> OfferState:
    cents = offer.placement_cents
    return OfferState(
        id=offer.pk,
        seller_id=offer.seller_id,
        service=offer.placement_type,
        cents=cents,
        currency=offer.currency,
        reviewed=offer.reviewed_at is not None,
        eur_cents=to_eur_cents(cents, offer.currency, rates) if cents is not None else None,
    )


def _within(prev: OfferState, cents: int, currency: str, pct: float) -> bool:
    """Цена продавца изменилась не больше чем на `pct` процентов от прежней."""
    if prev.cents is None or prev.currency != currency:
        return False
    if prev.cents == 0:
        return cents == 0
    return abs(cents - prev.cents) * 100 <= pct * prev.cents


def _capped(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {name: rows[:LIST_LIMIT], f"{name}_total": len(rows)}


def _chunks[T](values: Sequence[T] | Iterable[T]) -> list[list[T]]:
    items = list(values)
    return [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)]
