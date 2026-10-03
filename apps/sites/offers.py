"""Рабочая цена площадки и разбор предложений продавцов (ADR-043).

Рабочая цена — одно предложение площадки (`Site.price`), общее для всех
продуктов. Меняет её только человек: кнопкой в карточке, действием в
«Площадках» или в разборе загрузки. Каждая смена — заметка в истории.

Предложение, по которому ещё не решили, — `reviewed_at` пусто. Решение
«сделать рабочей» разбирает все предложения этой услуги у площадки,
«оставить как есть» — выбранные.

Пометки «новая цена» и «дешевле» считает `v_site_latest`, текущие
предложения — `v_site_offers`; здесь их не пересчитываем.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.sites.models import (
    PlacementType,
    Product,
    Seller,
    Site,
    SiteNote,
    SiteOffer,
    SitePrice,
)

TABLE_SOURCE = "таблица линкбилдинга"
# Валюты прайсов и оплат — на выбор в формах (загрузка прайса, размещение).
CURRENCIES = ("USD", "EUR", "GBP", "PLN", "CZK", "UAH")

_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(cents: int | None, currency: str) -> str:
    """Сумма для человека: 24000 EUR → «€240», 24050 → «€240.50», 900 PLN → «9 PLN»."""
    if cents is None:
        return ""
    units, rest = divmod(cents, 100)
    amount = f"{units}" if rest == 0 else f"{units}.{rest:02d}"
    symbol = _SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def describe(offer: SitePrice | SiteOffer) -> str:
    """«Collaborator · публикация €240» — для истории и сообщений."""
    seller = offer.seller_name if isinstance(offer, SiteOffer) else offer.seller.name
    service = PlacementType(offer.placement_type).label
    return f"{seller} · {service} {money(offer.placement_cents, offer.currency)}"


def add_note(
    site: Site | int,
    body: str,
    *,
    product: Product | None = None,
    author: Any = None,
    source: str | None = None,
) -> SiteNote:
    """Новая заметка в истории площадки. Автор — пользователь, источник — файл."""
    site_id = site if isinstance(site, int) else site.pk
    return SiteNote.objects.create(
        site_id=site_id, body=body, product=product, author=author, source=source
    )


def set_working_price(offer: SitePrice, *, author: Any = None, source: str | None = None) -> bool:
    """Делает предложение рабочей ценой площадки.

    Разбирает все неразобранные предложения той же услуги у площадки и
    пишет смену в историю. Предложение уже рабочее — только разбирает и
    возвращает False.
    """
    with transaction.atomic():
        # select_for_update — строка площадки заблокирована до конца транзакции:
        # два одновременных решения не перепишут рабочую цену друг другу.
        site = Site.all_objects.select_for_update().get(pk=offer.site_id)
        _review_service(site.pk, offer.placement_type)
        if site.price_id == offer.pk:
            return False
        old = (
            SitePrice.objects.select_related("seller").get(pk=site.price_id)
            if site.price_id is not None
            else None
        )
        site.price = offer
        site.save(update_fields=["price", "updated_at"])
        new = describe(offer)
        change = f"{describe(old)} → {new}" if old else f"рабочей стала {new}"
        add_note(site, f"Цена: {change}", author=author, source=source)
    return True


def keep_current(offers: Iterable[SitePrice | SiteOffer]) -> int:
    """«Оставить как есть» для выбранных предложений: разбирает, рабочую не трогает."""
    ids = [offer.pk for offer in offers]
    return SitePrice.objects.filter(pk__in=ids, reviewed_at__isnull=True).update(
        reviewed_at=timezone.now()
    )


def keep_current_for_sites(site_ids: Iterable[int]) -> int:
    """«Оставить как есть» в «Площадках»: разбирает все предложения площадок.

    Возвращает, у скольких площадок было что разбирать.
    """
    pending = SitePrice.objects.filter(site_id__in=list(site_ids), reviewed_at__isnull=True)
    sites = pending.values("site_id").distinct().count()
    pending.update(reviewed_at=timezone.now())
    return sites


@dataclass(frozen=True)
class Outcome:
    """Итог действия над площадками: у скольких сменилась цена, сколько пропущено."""

    changed: int
    skipped: int


def accept_new_prices(site_ids: Iterable[int], *, author: Any = None) -> Outcome:
    """«Принять новые цены»: рабочей становится текущая цена того же продавца за ту же услугу."""
    changed = skipped = 0
    for site in _sites_with_price(site_ids):
        working = site.price
        if working is None:
            continue
        current = _current(site.pk).filter(
            seller_id=working.seller_id, placement_type=working.placement_type
        )
        offer = current.first()
        if offer is not None and offer.pk != site.price_id:
            set_working_price(SitePrice.objects.get(pk=offer.pk), author=author)
            changed += 1
        else:
            skipped += 1
    return Outcome(changed, skipped)


def fix_seller(site_ids: Iterable[int], seller: Seller, *, author: Any = None) -> Outcome:
    """«Зафиксировать продавца…»: рабочей становится текущая цена этого продавца.

    Услуга — та же, что у рабочей цены; нет такой — публикация; нет и её —
    вставка ссылки. Нет предложения продавца или оно уже рабочее — пропуск.
    """
    changed = skipped = 0
    for site in Site.all_objects.filter(pk__in=list(site_ids)).select_related("price"):
        offers = {offer.placement_type: offer for offer in _current(site.pk).filter(seller=seller)}
        order = [PlacementType.GUEST_POST, PlacementType.LINK_INSERTION]
        if site.price is not None:
            order.insert(0, PlacementType(site.price.placement_type))
        offer = next((offers[service] for service in order if service in offers), None)
        if offer is not None and offer.pk != site.price_id:
            set_working_price(SitePrice.objects.get(pk=offer.pk), author=author)
            changed += 1
        else:
            skipped += 1
    return Outcome(changed, skipped)


def current_offers(site_ids: Iterable[int]) -> dict[int, list[SiteOffer]]:
    """Текущие предложения площадок одним запросом, дешёвые в евро — первыми."""
    result: dict[int, list[SiteOffer]] = {}
    queryset = SiteOffer.objects.filter(site_id__in=list(site_ids)).order_by(
        "site_id", "placement_eur_cents", "pk"
    )
    for offer in queryset:
        result.setdefault(offer.site_id, []).append(offer)
    return result


def _current(site_id: int) -> QuerySet[SiteOffer]:
    return SiteOffer.objects.filter(site_id=site_id).order_by("-checked_at", "-pk")


def _sites_with_price(site_ids: Iterable[int]) -> QuerySet[Site]:
    return Site.all_objects.filter(pk__in=list(site_ids), price__isnull=False).select_related(
        "price"
    )


def _review_service(site_id: int, placement_type: str) -> None:
    SitePrice.objects.filter(
        site_id=site_id, placement_type=placement_type, reviewed_at__isnull=True
    ).update(reviewed_at=timezone.now())
