"""Сверка продукта по снимку сделок: что показать на экранах (E11-01, ADR-080).

Снимок (`deals.snapshot`) → сделки продукта (по проекту) → площадки базы →
размещения продукта на них → сопоставление (`matching`) → группы
(`reconcile.Groups`). Запросов к базе — несколько на весь отчёт, не на сделку.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from django.db.models import Exists, OuterRef, Prefetch

from apps.content.domain_settings import collaborator_projects
from apps.keywords.matching import normalize
from apps.placements.models import InvoiceItem, InvoiceStatus, Placement, PlacementLink
from apps.sites.models import Product, Seller, Site

from .deals import Deal, Link, Money, snapshot
from .matching import Facts, Matched, SiteIndex, card_id, match, placements_by_site, safe_domain
from .reconcile import Groups, Row, reconcile, summary

CARD_URL = "https://collaborator.pro/creator/article/view?id={}"


@dataclass
class Report:
    product: Product
    deals: list[Deal]
    matched: list[Matched]
    groups: Groups
    fetched_at: dt.datetime | None
    # Сделки проектов, которые не привязаны ни к одному продукту: проект → (название, сделок).
    foreign: dict[int, tuple[str, int]] = field(default_factory=dict)
    domains: dict[int, str] = field(default_factory=dict)  # площадка → домен

    @property
    def summary(self) -> str:
        return summary(len(self.deals), self.groups)

    def row_of(self, deal_id: int) -> Row | None:
        for item in self.matched:
            if item.deal.id == deal_id and item.placement is not None:
                return reconcile(item)
        return None

    def matched_of(self, deal_id: int) -> Matched | None:
        return next((m for m in self.matched if m.deal.id == deal_id), None)


def owners(deals: Iterable[Deal]) -> dict[int, int | None]:
    """Сделка → продукт: проект записан у продукта, иначе проект с тем же названием."""
    explicit = collaborator_projects()
    names = {normalize(name): pk for pk, name in Product.objects.values_list("pk", "name")}

    def owner(deal: Deal) -> int | None:
        if deal.project_id is not None and deal.project_id in explicit:
            return explicit[deal.project_id]
        return names.get(normalize(deal.project_name)) if deal.project_name else None

    return {deal.id: owner(deal) for deal in deals}


def report(product: Product, *, site_id: int | None = None) -> Report:
    """Сверка сделок продукта; `site_id` — только сделки этой площадки (панель)."""
    deals, fetched = snapshot()
    owner = owners(deals)
    mine = [deal for deal in deals if owner[deal.id] == product.pk]
    foreign: dict[int, tuple[str, int]] = {}
    for deal in deals:
        if owner[deal.id] is None and deal.project_id is not None:
            name, count = foreign.get(deal.project_id, (deal.project_name, 0))
            foreign[deal.project_id] = (name, count + 1)
    sites = site_index(mine)
    if site_id is not None:
        mine = [deal for deal in mine if sites.find(deal) == site_id]
    site_ids = {found for deal in mine if (found := sites.find(deal)) is not None}
    facts = load_facts(product.pk, site_ids)
    matched = match(mine, sites, placements_by_site(facts))
    domains = dict(Site.all_objects.filter(pk__in=site_ids).values_list("pk", "domain"))
    return Report(product, mine, matched, Groups.of(matched), fetched, foreign, domains)


def site_index(deals: Sequence[Deal]) -> SiteIndex:
    """Площадки, на которые указывают сделки: по карточке и по домену."""
    cards = {deal.card_id or card_id(deal.card_url) for deal in deals} - {None}
    urls = {CARD_URL.format(card) for card in cards} | {d.card_url for d in deals if d.card_url}
    by_card: dict[int, list[tuple[int, str]]] = defaultdict(list)
    rows = Site.all_objects.filter(collaborator_url__in=urls).order_by("pk")
    for pk, url, domain in rows.values_list("pk", "collaborator_url", "domain"):
        number = card_id(url)
        if number is not None:
            by_card[number].append((pk, domain))
    domains = {safe_domain(deal.domain) for deal in deals} - {""}
    by_domain = dict(Site.all_objects.filter(domain__in=domains).values_list("domain", "pk"))
    return SiteIndex(by_card, by_domain)


def load_facts(product_id: int, site_ids: Iterable[int]) -> list[Facts]:
    """Размещения продукта на площадках — со ссылками по порядку."""
    collaborator = Seller.objects.filter(is_collaborator=True).values_list("pk", flat=True).first()
    in_invoice = InvoiceItem.objects.filter(placement=OuterRef("pk")).exclude(
        invoice__status=InvoiceStatus.CANCELLED
    )
    links = PlacementLink.objects.order_by("link_index", "pk")
    placements = (
        Placement.objects.filter(product_id=product_id, site_id__in=list(site_ids))
        .annotate(from_invoice=Exists(in_invoice))
        .prefetch_related(Prefetch("links", queryset=links))
        .order_by("pk")
    )
    return [facts_of(p, collaborator) for p in placements]


def facts_of(placement: Placement, collaborator_id: int | None) -> Facts:
    paid = None
    if placement.price_paid_cents is not None:
        paid = Money(placement.price_paid_cents, placement.currency or "EUR")
    return Facts(
        id=placement.pk,
        site_id=placement.site_id,
        status=str(placement.status),
        collaborator=collaborator_id is not None and placement.seller_id == collaborator_id,
        order_id=(placement.collaborator_order_id or "").strip(),
        article_url=(placement.article_url or "").strip(),
        paid=paid,
        from_invoice=bool(getattr(placement, "from_invoice", False)),
        announce=placement.announce_on_homepage,
        links=tuple(Link(link.anchor, link.target_url) for link in placement.links.all()),
        comment=placement.comment or "",
    )


def row_for(placement: Placement) -> tuple[Row | None, Report]:
    """Сделка размещения — для блока в панели: только сделки его площадки."""
    found = report(placement.product, site_id=placement.site_id)
    for item in found.matched:
        if item.placement is not None and item.placement.id == placement.pk:
            return reconcile(item), found
    return None, found
