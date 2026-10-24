"""Списки анкоров по регионам (ADR-075): какой список у размещения и где он нужен.

Регион размещения — список анкоров, из которого выбираются анкоры его ссылок.
Сохранённый в размещении — он; не выбирали — угадывается, как набор условий
серости (ADR-074): по анкорам уже выбранных ссылок, по языку площадки из
карточки Collaborator, по зоне домена; для языка нет списка — основной список
продукта (первый — США).

Список для языка — тот, у страны которого выдача на этом языке: португальский —
Бразилия, английский — США. Двух таких списков у продукта нет — значит, выбор
однозначен; если появятся (США и Британия), берётся заведённый раньше.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from apps.keywords.models import AnchorList
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites import countries
from apps.sites.languages import country_language, language_name, site_language
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
    """Список анкоров размещения и почему он — подпись рядом с выбором."""

    anchor_list: AnchorList | None
    reason: str


def choose(
    site: Site,
    lists: Sequence[AnchorList],
    *,
    stored: int | None = None,
    linked: Iterable[int] = (),
) -> Choice:
    """Список для карточки размещения.

    `stored` — список, сохранённый в размещении; `linked` — списки анкоров его
    ссылок. Списки — `AnchorList.of_product`: основной первым.
    """
    by_id = {item.pk: item for item in lists}
    if stored is not None and stored in by_id:
        return Choice(by_id[stored], "сохранён в размещении")
    used = {pk for pk in linked if pk in by_id}
    if len(used) == 1:
        return Choice(by_id[used.pop()], "по анкорам ссылок")
    language, how = site_language(site)
    for item in lists:
        if language is not None and country_language(item.country) == language:
            return Choice(item, how)
    if not lists:
        return Choice(None, "")
    main = lists[0]
    taken = f"взят «{main.region_name}»"
    if language is not None:
        return Choice(main, f"для языка «{language_name(language)}» списка нет — {taken}")
    return Choice(main, f"язык площадки не известен — {taken}")


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
    return choose(placement.site, lists, stored=placement.anchor_list_id, linked=linked)


def placement_countries(product_id: int) -> Counter[str]:
    """Сколько размещений продукта по регионам — размещено и ждут.

    Регион — страна списка размещения; не выбирали — угаданный, как в карточке.
    Без списков у продукта регионов нет — пустой счётчик.
    """
    lists = AnchorList.of_product(product_id)
    if not lists:
        return Counter()
    by_id = {item.pk: item for item in lists}
    placements = (
        Placement.objects.filter(product_id=product_id, status__in=COUNTED)
        .select_related("site")
        .only("pk", "anchor_list_id", "site__domain", "site__language")
    )
    linked: dict[int, set[int]] = {}
    for placement_id, list_id in (
        PlacementLink.objects.filter(
            placement__product_id=product_id, placement__anchor_list__isnull=True
        )
        .exclude(keyword=None)
        .values_list("placement_id", "keyword__anchor_list_id")
    ):
        linked.setdefault(placement_id, set()).add(list_id)
    found: Counter[str] = Counter()
    for placement in placements:
        stored = by_id.get(placement.anchor_list_id or 0)
        choice = (
            Choice(stored, "")
            if stored is not None
            else choose(placement.site, lists, linked=linked.get(placement.pk, ()))
        )
        if choice.anchor_list is not None:
            found[choice.anchor_list.country] += 1
    return found


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
