"""Слияние продавцов: дубли из разных выгрузок сводятся в одного (E1-17).

В файлах один и тот же человек написан по-разному — «Saket Aggrwal» и
«Saket Aggarwal», «WMLinks» и «WM Links», — и загрузка заводит двоих. Слияние
переносит на главного всё, что ссылается на продавца: цены, замеры метрик,
загрузки, размещения, счета. Ссылки на сами строки цен при этом не рвутся:
рабочая цена площадки и строки разбора загрузки показывают на те же строки,
у них лишь меняется продавец.

Одинаковые цены (та же площадка, услуга, дата, сумма и валюта) схлопываются в
одну: иначе у площадки задвоятся предложения. Прежде чем удалить лишнюю, всё,
что на неё ссылалось, переводится на оставшуюся.

Collaborator слить в другого нельзя: на нём держится каталог (ADR-041).
"""

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from apps.placements.models import Invoice, Placement
from apps.sites import latest
from apps.sites.models import Seller, Site, SiteMetric, SitePrice, Upload, UploadItem


class MergeError(Exception):
    """Слияние невозможно: причина — текстом для человека."""


@dataclass(frozen=True)
class Facts:
    """Чем продавец ценен — по этим числам выбирают главного."""

    seller: Seller
    prices: int = 0
    working: int = 0  # у скольких площадок его цена рабочая
    placements: int = 0
    invoices: int = 0
    metrics: int = 0
    uploads: int = 0
    last_price: dt.date | None = None

    @property
    def weight(self) -> int:
        """Насколько продавец «тяжёлый»: по нему предлагается главный."""
        return self.working * 10 + self.prices + self.placements + self.invoices


@dataclass
class Report:
    """Что сделало слияние — для сообщения человеку."""

    target: Seller
    sources: list[Seller] = field(default_factory=list)
    prices: int = 0
    duplicates: int = 0
    metrics: int = 0
    uploads: int = 0
    placements: int = 0
    invoices: int = 0
    deleted: int = 0


def facts(ids: Iterable[int]) -> list[Facts]:
    """Числа по каждому продавцу — одним запросом на таблицу, без запроса на строку."""
    sellers = {s.pk: s for s in Seller.objects.filter(pk__in=list(ids))}
    if not sellers:
        return []
    keys = list(sellers)
    prices = _counts(SitePrice.objects.filter(seller_id__in=keys))
    metrics = _counts(SiteMetric.objects.filter(seller_id__in=keys))
    uploads = _counts(Upload.objects.filter(seller_id__in=keys))
    placements = _counts(Placement.objects.filter(seller_id__in=keys))
    invoices = _counts(Invoice.objects.filter(seller_id__in=keys))
    working = _counts(Site.all_objects.filter(price__seller_id__in=keys), "price__seller_id")
    latest = {
        row["seller_id"]: row["last"]
        for row in SitePrice.objects.filter(seller_id__in=keys)
        .values("seller_id")
        .annotate(last=models.Max("checked_at"))
    }
    return [
        Facts(
            seller=seller,
            prices=prices.get(pk, 0),
            working=working.get(pk, 0),
            placements=placements.get(pk, 0),
            invoices=invoices.get(pk, 0),
            metrics=metrics.get(pk, 0),
            uploads=uploads.get(pk, 0),
            # Дата снимка — по времени проекта: в UTC полночь уехала бы на день назад.
            last_price=timezone.localdate(latest[pk]) if latest.get(pk) else None,
        )
        for pk, seller in sorted(sellers.items(), key=lambda item: item[1].name.lower())
    ]


def merge(
    target: Seller,
    sources: Sequence[Seller],
    *,
    delete_sources: bool = True,
    currency: str | None = None,
) -> Report:
    """Переносит всё со `sources` на `target`. Одной транзакцией.

    `delete_sources` — удалить слитых (по умолчанию) или оставить пустыми.
    `currency` — валюта главного, если выбрали не его собственную.
    """
    ids = [s.pk for s in sources if s.pk != target.pk]
    if not ids:
        raise MergeError("Нечего объединять: выберите хотя бы двух продавцов.")
    collaborator = [s for s in sources if s.is_collaborator and s.pk != target.pk]
    if collaborator:
        raise MergeError(
            "Collaborator нельзя слить в другого продавца: на нём держится каталог. "
            "Сделайте главным его."
        )
    report = Report(target=target, sources=[s for s in sources if s.pk in ids])
    with transaction.atomic():
        # Площадки слитых продавцов — до переноса: по ним пересчитаем копию
        # «площадки на сегодня». Без этого в ней остались бы ссылки на
        # удалённого продавца и на схлопнутые цены, а удаление продавца упало
        # бы о PROTECT со стороны site_latest (E1-11).
        touched = set(
            SitePrice.objects.filter(seller_id__in=ids).values_list("site_id", flat=True)
        ) | set(SiteMetric.objects.filter(seller_id__in=ids).values_list("site_id", flat=True))
        report.duplicates = _collapse_duplicates(target, ids)
        report.prices = SitePrice.objects.filter(seller_id__in=ids).update(seller=target)
        report.metrics = SiteMetric.objects.filter(seller_id__in=ids).update(seller=target)
        report.uploads = Upload.objects.filter(seller_id__in=ids).update(seller=target)
        report.placements = Placement.objects.filter(seller_id__in=ids).update(seller=target)
        report.invoices = Invoice.objects.filter(seller_id__in=ids).update(seller=target)
        _keep_notes(target, report.sources, currency)
        latest.refresh(touched)
        if delete_sources:
            report.deleted = Seller.objects.filter(pk__in=ids).delete()[0]
    return report


def _counts(queryset: models.QuerySet[Any], column: str = "seller_id") -> dict[int, int]:
    rows = queryset.order_by().values(column).annotate(total=models.Count("pk"))
    return {row[column]: row["total"] for row in rows}


def _collapse_duplicates(target: Seller, source_ids: Sequence[int]) -> int:
    """Одинаковые цены главного и слитых — в одну; ссылки переводятся на неё.

    Ключ дубля — площадка, услуга, дата снимка, сумма и валюта: это один и тот
    же факт, записанный дважды под разными именами продавца.
    """
    kept: dict[tuple[Any, ...], int] = {
        _key(price): price.pk for price in SitePrice.objects.filter(seller=target)
    }
    pairs: list[tuple[int, int]] = []
    for price in SitePrice.objects.filter(seller_id__in=source_ids).order_by("pk"):
        key = _key(price)
        twin = kept.get(key)
        if twin is None:
            kept[key] = price.pk
            continue
        pairs.append((price.pk, twin))
    for extra, keep in pairs:
        Site.all_objects.filter(price_id=extra).update(price_id=keep)
        UploadItem.objects.filter(price_id=extra).update(price_id=keep)
        UploadItem.objects.filter(ref_price_id=extra).update(ref_price_id=keep)
    if pairs:
        SitePrice.objects.filter(pk__in=[extra for extra, _ in pairs]).delete()
    return len(pairs)


def _key(price: SitePrice) -> tuple[Any, ...]:
    return (
        price.site_id,
        price.placement_type,
        price.checked_at,
        price.placement_cents,
        price.currency,
    )


def _keep_notes(target: Seller, sources: Sequence[Seller], currency: str | None) -> None:
    """Имена, контакты и заметки слитых — в заметки главного: ничего не теряется."""
    lines = [f"Объединены {_names(sources)} → {target.name}."]
    for seller in sources:
        parts = [part for part in (seller.contacts, seller.notes) if part]
        if parts:
            lines.append(f"{seller.name}: {' · '.join(parts)}")
    target.notes = "\n".join(filter(None, [target.notes, *lines]))
    fields = ["notes"]
    if currency and currency != target.currency:
        target.currency = currency
        fields.append("currency")
    if not target.contacts:
        contacts = [seller.contacts for seller in sources if seller.contacts]
        if contacts:
            target.contacts = contacts[0]
            fields.append("contacts")
    target.save(update_fields=fields)


def _names(sellers: Sequence[Seller]) -> str:
    return ", ".join(f"«{seller.name}»" for seller in sellers)
