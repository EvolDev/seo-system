"""Списки анкоров по регионам (ADR-075): какой список у размещения и где он нужен.

Регион размещения — список анкоров, из которого выбираются анкоры его ссылок.
Сохранённый в размещении — он; не выбирали — угадывается, как набор условий
серости (ADR-074): по анкорам уже выбранных ссылок, по языку площадки из
карточки Collaborator, по зоне домена, по топ-региону площадки из Ahrefs
(ADR-076); для языка нет списка — основной список продукта (первый — США).

Список для языка — тот, у страны которого выдача на этом языке: португальский —
Бразилия, английский — США. Двух таких списков у продукта нет — значит, выбор
однозначен; если появятся (США и Британия), берётся заведённый раньше.

Тот же регион размещения — у фильтра «Регион» в «Размещениях» и у колонок
выгрузки «Region» и «Region Traff» (ADR-076). Там регион только известный:
основной список «по умолчанию» (ничто не подсказало страну) — регион не
определён, колонки пустые.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from apps.keywords.models import AnchorList
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites import countries
from apps.sites.languages import (
    country_language,
    language_name,
    site_language,
    top_geo_of,
    top_geos,
)
from apps.sites.models import Site

# Размещения, которые считаются в долях: размещено и ждут — как ссылки анкоров.
COUNTED = (
    PlacementStatus.PLACED,
    PlacementStatus.IN_WORK,
    PlacementStatus.ORDERED,
    PlacementStatus.WRITING,
)


@dataclass(frozen=True)
class Choice:
    """Список анкоров размещения и почему он — подпись рядом с выбором.

    `fallback` — основной список взят по умолчанию: ни сохранённого выбора, ни
    ссылок, ни языка с его списком. Для анкоров его хватает, но регион не известен.
    """

    anchor_list: AnchorList | None
    reason: str
    fallback: bool = False


def choose(
    site: Site,
    lists: Sequence[AnchorList],
    *,
    stored: int | None = None,
    linked: Iterable[int] = (),
    top_geo: str | None = None,
) -> Choice:
    """Список для карточки размещения.

    `stored` — список, сохранённый в размещении; `linked` — списки анкоров его
    ссылок; `top_geo` — топ-регион площадки. Списки — `AnchorList.of_product`:
    основной первым.
    """
    by_id = {item.pk: item for item in lists}
    if stored is not None and stored in by_id:
        return Choice(by_id[stored], "сохранён в размещении")
    used = {pk for pk in linked if pk in by_id}
    if len(used) == 1:
        return Choice(by_id[used.pop()], "по анкорам ссылок")
    language, how = site_language(site, top_geo)
    for item in lists:
        if language is not None and country_language(item.country) == language:
            return Choice(item, how)
    if not lists:
        return Choice(None, "")
    main = lists[0]
    taken = f"взят «{main.region_name}»"
    if language is not None:
        reason = f"для языка «{language_name(language)}» списка нет — {taken}"
        return Choice(main, reason, fallback=True)
    return Choice(main, f"язык площадки не известен — {taken}", fallback=True)


def for_placement(placement: Placement, lists: Sequence[AnchorList] | None = None) -> Choice:
    """Список размещения: сохранённый, иначе угаданный по ссылкам и площадке."""
    if lists is None:
        lists = AnchorList.of_product(placement.product_id)
    linked: list[int] = []
    if placement.pk is not None and placement.anchor_list_id is None:
        linked = list(
            placement.links.exclude(keyword=None)
            .values_list("keyword__anchor_list_id", flat=True)
            .distinct()
        )
    return choose(
        placement.site,
        lists,
        stored=placement.anchor_list_id,
        linked=linked,
        top_geo=top_geo_of(placement.site_id) if placement.anchor_list_id is None else None,
    )


def effective_lists(placements: Sequence[Placement]) -> dict[int, AnchorList | None]:
    """Список каждого размещения разом — как `for_placement`, без запроса на строку.

    Размещения — с площадкой (`select_related("site")`). Списков у продукта нет —
    None.
    """
    return {pk: choice.anchor_list for pk, choice in effective_choices(placements).items()}


def effective_choices(placements: Sequence[Placement]) -> dict[int, Choice]:
    """Выбор списка каждого размещения разом — с признаком «по умолчанию»."""
    lists = {
        product_id: AnchorList.of_product(product_id)
        for product_id in {placement.product_id for placement in placements}
    }
    stored = {item.pk for items in lists.values() for item in items}
    guessed = [p for p in placements if p.anchor_list_id not in stored]
    linked: dict[int, set[int]] = defaultdict(set)
    for placement_id, list_id in (
        PlacementLink.objects.filter(placement_id__in=[p.pk for p in guessed])
        .exclude(keyword=None)
        .values_list("placement_id", "keyword__anchor_list_id")
    ):
        linked[placement_id].add(list_id)
    geos = top_geos(p.site_id for p in guessed)
    return {
        placement.pk: choose(
            placement.site,
            lists[placement.product_id],
            stored=placement.anchor_list_id,
            linked=linked.get(placement.pk, ()),
            top_geo=geos.get(placement.site_id),
        )
        for placement in placements
    }


def known_region(choice: Choice) -> str | None:
    """Регион размещения, если он известен: страна списка; по умолчанию — None."""
    if choice.anchor_list is None or choice.fallback:
        return None
    return choice.anchor_list.country


def placement_regions(placement_ids: Iterable[int]) -> dict[int, str | None]:
    """Известный регион размещений — страна их списка анкоров (`br`, `us`).

    Не известен (списков нет, основной взят по умолчанию) — None: в выгрузке пусто.
    """
    placements = list(
        Placement.objects.filter(pk__in=list(placement_ids))
        .select_related("site")
        .only("pk", "product_id", "anchor_list_id", "site__domain", "site__language")
    )
    return {pk: known_region(choice) for pk, choice in effective_choices(placements).items()}


def placement_countries(product_id: int) -> Counter[str]:
    """Сколько размещений продукта по регионам — размещено и ждут.

    Регион — страна списка размещения; не выбирали — угаданный, как в карточке.
    Без списков у продукта регионов нет — пустой счётчик.
    """
    placements = list(
        Placement.objects.filter(product_id=product_id, status__in=COUNTED)
        .select_related("site")
        .only("pk", "product_id", "anchor_list_id", "site__domain", "site__language")
    )
    return Counter(
        anchor_list.country
        for anchor_list in effective_lists(placements).values()
        if anchor_list is not None
    )


def options(lists: Sequence[AnchorList]) -> list[dict[str, str]]:
    """Списки для выбора с флагами: id, страна, название, флаг."""
    return [
        {
            "id": str(item.pk),
            "country": item.country,
            "name": item.region_name,
            "flag": countries.flag_code(item.country) or "",
        }
        for item in lists
    ]
