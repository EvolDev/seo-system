"""Колонка «другие продукты»: кто ещё работает с этой площадкой (E1-19, ADR-063).

Рабочий продукт — рамка списка, поэтому чужое размещение видно не строкой, а
колонкой: имя продукта ссылкой на его карточку. Колонка одна и та же в
«Площадках» и в «Размещениях», на одном и том же месте.

Считается одним запросом на страницу списка, как предложения цен
(`OffersChangeList`): колонка строки берёт готовое и в базу сама не ходит.
Состав тот же, что у `other_products` в `v_product_site_latest`, откуда берёт
колонку выгрузка: размещения в любом статусе, по одному — самому свежему — на
продукт.
"""

from collections.abc import Iterable

from django.db.models import F
from django.urls import reverse
from django.utils.html import format_html_join
from django.utils.safestring import SafeString

from apps.placements.models import Placement

# Площадка → её размещения: продукт, его название, самое свежее размещение.
Row = tuple[int, str, int]
BySite = dict[int, list[Row]]


def by_site(site_ids: Iterable[int]) -> BySite:
    """Размещения площадок страницы: по одному на продукт, продукты по алфавиту."""
    wanted = sorted(set(site_ids))
    if not wanted:
        return {}
    rows = (
        Placement.objects.filter(site_id__in=wanted)
        .order_by("site_id", "product__name", F("published_at").desc(nulls_last=True), "-pk")
        .values_list("site_id", "product_id", "product__name", "pk")
    )
    found: BySite = {}
    for site_id, product_id, name, placement_id in rows:
        products = found.setdefault(site_id, [])
        # У продукта на площадке бывает несколько статей — берём первую, свежую.
        if products and products[-1][0] == product_id:
            continue
        products.append((product_id, name, placement_id))
    return found


def others(rows: list[Row], working: int | None) -> list[tuple[str, int]]:
    """Чужие продукты строки: все, кроме того, под которым работаем."""
    return [(name, pk) for product_id, name, pk in rows if product_id != working]


def own(rows: list[Row], working: int | None) -> int | None:
    """Размещение рабочего продукта на этой площадке; его нет — None."""
    return next((pk for product_id, _, pk in rows if product_id == working), None)


def cell(found: list[tuple[str, int]]) -> SafeString | str:
    """Имена продуктов ссылками на их карточки размещений — панелью справа."""
    if not found:
        return ""
    return format_html_join(
        ", ",
        '<a href="{}" data-panel title="Карточка размещения {}">{}</a>',
        ((card_url(placement_id), name, name) for name, placement_id in found),
    )


def card_url(placement_id: int) -> str:
    return reverse("admin:placements_placement_change", args=[placement_id])


def add_url(site_id: int, product_id: int) -> str:
    """Пустая форма размещения под рабочий продукт: площадка и продукт подставлены."""
    return f"{reverse('admin:placements_placement_add')}?site={site_id}&product={product_id}"
