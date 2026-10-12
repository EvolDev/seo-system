"""Снятие с продажи (ADR-068): площадки больше нет в полном каталоге продавца.

Каталог Collaborator — вся его база на дату выгрузки. Если у площадки в базе
есть текущее предложение Collaborator, а в файле её нет, он её больше не
продаёт. «Снять с продажи» пишет такому предложению снимок без цены на дату
файла: прежние цены остаются в истории, а текущим предложением снимок не
считается (`v_site_offers`). Поэтому из фильтров по цене и продавцу, из
«дешевле» и «новой цены» площадка уходит сама. Рабочая цена этого продавца
снимается — заметкой, как любая смена рабочей цены (ADR-043).

Выгрузку с фильтром по файлу от полного каталога не отличить, поэтому
снятие — только кнопкой с галочкой «это полный каталог» (решение пользователя
12.10.2026). Площадку с заявкой в работе не трогаем: цена уже договорённая
(ADR-044). Снимается предложение старее даты файла: старый файл после нового
ничего не снимает. Вернулась площадка в следующем каталоге — обычная новая
цена; без рабочей цены она становится рабочей сама (ADR-044).
"""

import datetime as dt
import logging
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from django.db import connection
from django.utils import timezone

from apps.sites import latest, offers
from apps.sites.models import MetricSource, Site, SiteNote, SitePrice, Upload
from apps.sites.uploads.plan import LIST_LIMIT, ORDER_IN_WORK, start_of_day

logger = logging.getLogger(__name__)

BATCH = 1000

# Текущие предложения продавца (последний снимок на площадку и услугу) у площадок,
# которых нет среди доменов файла. Домены файла — множеством через unnest: сравнение
# `= ANY` с массивом на 45 000 строк перебирало бы его на каждую площадку.
_FIND = """
WITH file(domain) AS (SELECT unnest(%(domains)s::text[])),
cur AS (
    SELECT DISTINCT ON (p.site_id, p.placement_type)
           p.id, p.site_id, p.placement_type, p.withdrawn, p.checked_at
    FROM site_prices p
    WHERE p.seller_id = %(seller)s
    ORDER BY p.site_id, p.placement_type, p.checked_at DESC, p.id DESC
)
SELECT cur.id, s.id, s.domain,
       coalesce(pr.seller_id = %(seller)s AND pr.placement_type = cur.placement_type, false),
       EXISTS (SELECT 1 FROM placements x
               WHERE x.site_id = s.id AND x.status::text = ANY(%(frozen)s)),
       EXISTS (SELECT 1 FROM v_site_offers o
               WHERE o.site_id = s.id AND o.seller_id <> %(seller)s)
FROM cur
JOIN sites s ON s.id = cur.site_id AND NOT s.is_deleted
LEFT JOIN site_prices pr ON pr.id = s.price_id
WHERE NOT cur.withdrawn AND cur.checked_at < %(before)s
  AND NOT EXISTS (SELECT 1 FROM file f WHERE f.domain = s.domain)
ORDER BY s.domain, cur.id
"""


@dataclass(frozen=True)
class Gone:
    """Текущее предложение продавца у площадки, которой нет в файле."""

    offer_id: int
    site_id: int
    domain: str
    working: bool  # это рабочая цена площадки: снимется вместе с предложением
    frozen: bool  # по площадке заявка в работе — не трогаем
    others: bool  # у площадки есть текущие предложения других продавцов


@dataclass(frozen=True)
class Absent:
    offers: tuple[Gone, ...]

    def summary(self, *, file_sites: int) -> dict[str, Any]:
        """Сводка для страницы каталога: сколько снимется и что станет с площадками.

        `looks_partial` — снимется больше площадок, чем есть в файле: похоже на
        выгрузку с фильтром, такую снимать нельзя.
        """
        by_site: dict[int, list[Gone]] = {}
        for gone in self.offers:
            by_site.setdefault(gone.site_id, []).append(gone)
        counts: Counter[str] = Counter()
        rows: list[dict[str, str]] = []
        for items in by_site.values():
            first = items[0]
            if first.frozen:
                counts["frozen"] += 1
                notes = ["заявка в работе — не трогаем"]
            else:
                counts["total"] += 1
                notes = []
                if any(gone.working for gone in items):
                    counts["working"] += 1
                    notes.append("рабочая цена снимется")
                if first.others:
                    counts["others"] += 1
                    notes.append("есть другие продавцы")
                else:
                    counts["stop"] += 1
                    notes.append("больше никто не продаёт")
            rows.append({"domain": first.domain, "note": ", ".join(notes)})
        return {
            "total": counts["total"],
            "working": counts["working"],
            "others": counts["others"],
            "stop": counts["stop"],
            "frozen": counts["frozen"],
            "looks_partial": counts["total"] > file_sites,
            "list": rows[:LIST_LIMIT],
            "list_total": len(rows),
        }


def find(seller_id: int, domains: Collection[str], before: dt.datetime) -> Absent:
    """Текущие предложения продавца у площадок, которых нет в файле, старее `before`."""
    with connection.cursor() as cursor:
        cursor.execute(
            _FIND,
            {
                "domains": sorted(domains),
                "seller": seller_id,
                "frozen": [str(status) for status in ORDER_IN_WORK],
                "before": before,
            },
        )
        rows = cursor.fetchall()
    return Absent(tuple(Gone(*row) for row in rows))


def for_upload(upload: Upload, domains: Collection[str]) -> Absent:
    """Кого нет в этом файле — на дату его цен."""
    if upload.seller_id is None:
        raise ValueError("снятие с продажи — только у загрузки с продавцом")
    return find(upload.seller_id, domains, start_of_day(upload.prices_date))


def run(upload: Upload, domains: Collection[str]) -> dict[str, Any]:
    """Снять с продажи. Транзакцию и журнал загрузки (для отмены) открывает `service.write`."""
    absent = for_upload(upload, domains)
    todo = [gone for gone in absent.offers if not gone.frozen]
    if absent.summary(file_sites=len(domains))["looks_partial"]:
        # Кнопку на такой файл экран не даёт; это страховка от старой страницы.
        logger.warning(
            "снятие с продажи отклонено: похоже на выгрузку с фильтром",
            extra={"upload_id": upload.pk, "file_sites": len(domains)},
        )
        return {"counts": {"sites_withdrawn": 0, "refused_partial": 1}}
    now = timezone.now()
    day = start_of_day(upload.prices_date)
    site_ids = sorted({gone.site_id for gone in todo})
    previous = SitePrice.objects.in_bulk([gone.offer_id for gone in todo])
    SitePrice.objects.bulk_create(
        [
            SitePrice(
                site_id=offer.site_id,
                seller_id=offer.seller_id,
                placement_type=offer.placement_type,
                currency=offer.currency,
                # Тип ссылки — условие продавца: вернётся площадка — уточнение
                # человека перейдёт и на новую цену (ADR-066).
                link_type=offer.link_type,
                link_type_manual=offer.link_type_manual,
                source=MetricSource.CSV_IMPORT,
                withdrawn=True,
                reviewed_at=now,
                checked_at=day,
            )
            for offer in previous.values()
        ],
        batch_size=BATCH,
    )
    # Неразобранные цены этого продавца у этих площадок: решать по ним уже нечего.
    for start in range(0, len(site_ids), BATCH):
        SitePrice.objects.filter(
            site_id__in=site_ids[start : start + BATCH],
            seller_id=upload.get_seller().pk,
            reviewed_at__isnull=True,
        ).update(reviewed_at=now)
    cleared = _clear_working(upload, sorted({gone.site_id for gone in todo if gone.working}), now)
    # Копия «площадки на сегодня» (ADR-065): цена, пометки и «не продаётся» — сразу.
    latest.refresh(site_ids)
    return {
        "counts": {
            "sites_withdrawn": len(site_ids),
            "offers_withdrawn": len(previous),
            "working_cleared": cleared,
            "skipped_frozen": len({gone.site_id for gone in absent.offers if gone.frozen}),
        }
    }


def _clear_working(upload: Upload, site_ids: list[int], now: dt.datetime) -> int:
    """Рабочая цена снятого предложения снимается; в истории — заметка (ADR-043)."""
    if not site_ids:
        return 0
    when = f"каталог {upload.get_seller().name} от {upload.prices_date:%d.%m.%Y}"
    notes: list[SiteNote] = []
    for start in range(0, len(site_ids), BATCH):
        chunk = site_ids[start : start + BATCH]
        for site in Site.all_objects.filter(pk__in=chunk).select_related("price__seller"):
            if site.price is not None:
                body = f"Цена: {offers.describe(site.price)} → снята с продажи ({when})"
                notes.append(SiteNote(site_id=site.pk, body=body, source=upload.file_name))
        Site.all_objects.filter(pk__in=chunk).update(price=None, updated_at=now)
    SiteNote.objects.bulk_create(notes, batch_size=BATCH)
    return len(notes)
