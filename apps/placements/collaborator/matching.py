"""Какое размещение у сделки Collaborator (E11-01, ADR-080).

Продукт сделки — по проекту (`COLLABORATOR_PROJECTS` продукта, иначе проект с
тем же названием, что у продукта). Площадка — по карточке Collaborator
(`creator.id` против `sites.collaborator_url`), иначе по домену. Одна карточка
бывает у двух площадок (vilniaus.diena.lt и diena.lt) — тогда та, у которой
тот же домен.

Размещение — среди размещений продукта на площадке, в три прохода по всем
сделкам сразу, чтобы одно размещение не досталось двум сделкам:
1. номер заявки размещения — номер сделки;
2. адрес статьи — тот же (`indexation.normalize_url`: без схемы, `www.`, `/` в
   конце и меток вроде `?srsltid`);
3. оставшиеся: на площадке ровно одно свободное размещение с продавцом
   Collaborator и ровно одна живая сделка без размещения — они пара. Живые
   сделки идут вперёд отменённых: отменённая берёт размещение, только если
   живой на него нет.

Не нашлось — сделка без размещения: площадки нет в базе, размещений нет, их
несколько (какое — неясно) или одно на несколько сделок (talkandroid.com: две
сделки, в системе одно размещение со ссылками обеих). Отменённые сделки без
размещения в сверку не идут: заводить по ним нечего.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from apps.placements.indexation import normalize_url
from apps.sites.domains import normalize_domain

from .deals import Deal, Link, Money

_CARD_ID = re.compile(r"[?&]id=(\d+)")


@dataclass(frozen=True)
class Facts:
    """Размещение — то, что о нём нужно сверке."""

    id: int
    site_id: int
    status: str
    collaborator: bool  # продавец размещения — Collaborator
    order_id: str = ""
    article_url: str = ""
    paid: Money | None = None
    from_invoice: bool = False  # «Заплачено» ставит счёт (ADR-055)
    announce: bool | None = None
    links: tuple[Link, ...] = ()
    comment: str = ""


class How(StrEnum):
    ORDER = "order"
    ARTICLE = "article"
    SINGLE = "single"


HOW_TEXT = {
    How.ORDER: "по номеру заявки",
    How.ARTICLE: "по адресу статьи",
    How.SINGLE: "единственное размещение площадки у Collaborator",
}


class Miss(StrEnum):
    NO_SITE = "no_site"  # площадки нет в базе
    NO_PLACEMENT = "no_placement"  # у продукта на площадке размещений нет
    TAKEN = "taken"  # размещения площадки уже у других сделок
    OTHER_SELLER = "other_seller"  # свободные размещения — через других продавцов
    MANY = "many"  # свободных с Collaborator несколько — какое, неясно
    SHARED = "shared"  # одно свободное на несколько сделок


@dataclass(frozen=True)
class Matched:
    deal: Deal
    site_id: int | None
    placement: Facts | None = None
    how: How | None = None
    miss: Miss | None = None
    # Размещения, из которых не выбрать (MANY) или которое делят сделки (SHARED).
    candidates: tuple[int, ...] = ()


def card_id(url: str | None) -> int | None:
    """Номер карточки из адреса `…/creator/article/view?id=630296`."""
    found = _CARD_ID.search(url or "")
    return int(found.group(1)) if found else None


def safe_domain(value: str) -> str:
    try:
        return normalize_domain(value)
    except ValueError:
        return ""


class SiteIndex:
    """Площадки базы по карточке Collaborator и по домену."""

    def __init__(
        self,
        cards: Mapping[int, Sequence[tuple[int, str]]],
        domains: Mapping[str, int],
    ) -> None:
        self._cards = cards
        self._domains = domains

    def find(self, deal: Deal) -> int | None:
        domain = safe_domain(deal.domain)
        card = deal.card_id if deal.card_id is not None else card_id(deal.card_url)
        on_card = self._cards.get(card, ()) if card is not None else ()
        if len(on_card) == 1:
            return on_card[0][0]
        for site_id, site_domain in on_card:
            if site_domain == domain:
                return site_id
        return self._domains.get(domain) if domain else None


def match(
    deals: Sequence[Deal],
    sites: SiteIndex,
    placements: Mapping[int, Sequence[Facts]],
) -> list[Matched]:
    """Размещение каждой сделки — в порядке `deals`."""
    site_of = {deal.id: sites.find(deal) for deal in deals}
    found: dict[int, tuple[Facts, How]] = {}
    taken: set[int] = set()

    def of_site(deal: Deal) -> Sequence[Facts]:
        site_id = site_of[deal.id]
        return placements.get(site_id, ()) if site_id is not None else ()

    def claim(deal: Deal, facts: Facts, how: How) -> None:
        found[deal.id] = (facts, how)
        taken.add(facts.id)

    for deal in deals:
        number = str(deal.id)
        for facts in of_site(deal):
            if facts.id not in taken and facts.order_id.strip() == number:
                claim(deal, facts, How.ORDER)
                break
    for deal in deals:
        if deal.id in found or not deal.article_url:
            continue
        wanted = normalize_url(deal.article_url)
        for facts in of_site(deal):
            if (
                facts.id not in taken
                and facts.article_url
                and normalize_url(facts.article_url) == wanted
            ):
                claim(deal, facts, How.ARTICLE)
                break

    misses: dict[int, tuple[Miss, tuple[int, ...]]] = {}
    waiting: dict[int, list[Deal]] = defaultdict(list)
    for deal in sorted(deals, key=lambda d: (d.cancelled, d.id)):
        site_id = site_of[deal.id]
        if deal.id in found:
            continue
        if site_id is None:
            misses[deal.id] = (Miss.NO_SITE, ())
        else:
            waiting[site_id].append(deal)
    for site_id, group in waiting.items():
        _pair(group, placements.get(site_id, ()), taken, found, misses)

    result = []
    for deal in deals:
        site_id = site_of[deal.id]
        if deal.id in found:
            facts, how = found[deal.id]
            result.append(Matched(deal, site_id, facts, how=how))
        else:
            miss, candidates = misses.get(deal.id, (Miss.NO_PLACEMENT, ()))
            result.append(Matched(deal, site_id, miss=miss, candidates=candidates))
    return result


def _pair(
    group: list[Deal],
    site: Sequence[Facts],
    taken: set[int],
    found: dict[int, tuple[Facts, How]],
    misses: dict[int, tuple[Miss, tuple[int, ...]]],
) -> None:
    """Третий проход для одной площадки: единственное свободное — единственной сделке."""
    unclaimed = [f for f in site if f.id not in taken]
    free = [f for f in unclaimed if f.collaborator]
    live = [deal for deal in group if not deal.cancelled]
    miss: tuple[Miss, tuple[int, ...]] | None = None
    if not site:
        miss = (Miss.NO_PLACEMENT, ())
    elif not unclaimed:
        miss = (Miss.TAKEN, tuple(f.id for f in site))
    elif not free:
        miss = (Miss.OTHER_SELLER, tuple(f.id for f in unclaimed))
    elif len(free) > 1:
        miss = (Miss.MANY, tuple(f.id for f in free))
    if miss is not None:
        for deal in group:
            misses[deal.id] = miss
        return
    only = free[0]
    contenders = live or group
    if len(contenders) == 1:
        found[contenders[0].id] = (only, How.SINGLE)
        return
    for deal in contenders:
        misses[deal.id] = (Miss.SHARED, (only.id,))


def placements_by_site(facts: Iterable[Facts]) -> dict[int, list[Facts]]:
    grouped: dict[int, list[Facts]] = defaultdict(list)
    for item in facts:
        grouped[item.site_id].append(item)
    return grouped
