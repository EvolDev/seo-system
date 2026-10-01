"""Запись плана загрузки в базу — в одной транзакции её открывает `service.write`.

По видам данных (ADR-033, ADR-043, ADR-044):
- площадки — одна на домен: новых нет — создаются со статусом «Новая» под
  все продукты. Факты карточки Collaborator перезаписываются непустыми
  значениями каталога; из прайса продавца — только тип ссылки, и только
  если он пуст;
- предложения — снимок на дату цен: та же дата обновляется на месте,
  кроме рабочей цены — та остаётся, другая цена встаёт рядом новой строкой;
- рабочая цена — по решению плана; старые неразобранные цены того же
  продавца за ту же услугу разбираются сами: их сменила новая;
- метрики продавца — снимок с продавцом на дату цен;
- наша заметка из файла — в историю, тот же текст второй раз не пишется;
- все площадки файла — в рабочий список «<продавец> · <дата>».
"""

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from django.utils import timezone

from apps.sites.models import (
    MetricSource,
    Seller,
    Site,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
    Upload,
    UploadItem,
    ensure_product_sites,
)
from apps.sites.uploads.plan import LIST_LIMIT, Plan, PlanItem, start_of_day

# Факты карточки Collaborator — поля `sites`, их пишет только каталог.
CARD_FIELDS = (
    "source",
    "collaborator_url",
    "language",
    "languages",
    "topics",
    "declared_topics",
    "site_type",
    "links_allowed",
    "link_type",
    "marks_as_ad",
)
PRICE_FIELDS = (
    "placement_cents",
    "currency",
    "gray_cents",
    "writing_cents",
    "announce_cents",
    "extra",
)
METRIC_FIELDS = ("dr", "organic_traffic", "total_keywords")
BATCH = 1000


def list_name(seller: Seller, day: dt.date) -> str:
    return f"{seller.name} · {day:%d.%m.%Y}"


class Part(StrEnum):
    """Что записать из файла — галочки у кнопок каталога (ADR-044)."""

    PRICES = "prices"
    METRICS = "metrics"
    CARD = "card"

    @property
    def label(self) -> str:
        return {
            "prices": "цены",
            "metrics": "метрики: DR, трафик, ключи",
            "card": "описание: тематики, языки, тип сайта, dofollow, реклама",
        }[self.value]

    @property
    def short(self) -> str:
        return {"prices": "цены", "metrics": "метрики", "card": "описание"}[self.value]


ALL_PARTS = frozenset(Part)


class Writer:
    """Один проход записи. Счётчики — в `counts`, они же — итог для человека.

    `parts` — что писать: цены (предложения, рабочая цена, разбор),
    метрики, карточку. Без карточки новая площадка заводится с одним
    доменом и источником. Площадки всегда попадают в рабочий список.
    """

    def __init__(self, upload: Upload, plan: Plan, parts: frozenset[Part] = ALL_PARTS) -> None:
        self.upload = upload
        self.plan = plan
        self.parts = parts
        self.seller = upload.seller
        self.checked_at = start_of_day(upload.prices_date)
        self.now = timezone.now()
        self.counts: Counter[str] = Counter()
        self.facts_changed: list[str] = []
        self.facts_changed_total = 0
        self.site_ids: dict[str, int] = {domain: s.id for domain, s in plan.sites.items()}
        self.created: set[str] = set()

    def run(self) -> dict[str, Any]:
        self._create_sites()
        if Part.CARD in self.parts:
            self._update_sites()
        if Part.PRICES in self.parts:
            prices = self._write_offers()
            self._move_working(prices)
            self._supersede()
        if Part.METRICS in self.parts:
            self._metrics()
        self._notes()
        site_list = self._site_list()
        if Part.PRICES in self.parts:
            self._items(prices)
        self.upload.site_list = site_list
        return {
            "counts": dict(self.counts),
            "list": site_list.name,
            "facts_changed": self.facts_changed,
            "facts_changed_total": self.facts_changed_total,
        }

    # --- Площадки ---

    def _create_sites(self) -> None:
        new: list[Site] = []
        seen: set[str] = set()
        for item in self.plan.items:
            record = item.record
            if item.site is not None or record.domain in seen:
                continue
            seen.add(record.domain)
            fields: dict[str, object]
            if Part.CARD not in self.parts:
                fields = {"source": record.card["source"] if record.card else self.seller.name}
            elif record.card is not None:
                fields = {k: v for k, v in record.card.items() if k in CARD_FIELDS}
            else:
                fields = {"source": self.seller.name, "link_type": record.link_type}
            new.append(Site(domain=record.domain, **fields))
        # Удалённая раньше площадка не создаётся заново (домен уникален и среди
        # удалённых), а возвращается: пометка снимается, описание обновляется.
        restored = [site for site in new if site.domain in self.plan.restore]
        new = [site for site in new if site.domain not in self.plan.restore]
        for site in restored:
            site.pk = self.plan.restore[site.domain]
            site.is_deleted = False
            site.updated_at = self.now
            self.site_ids[site.domain] = site.pk
        if restored:
            names = {"is_deleted", "updated_at"} | {
                name for site in restored for name in CARD_FIELDS if getattr(site, name) is not None
            }
            Site.all_objects.bulk_update(restored, sorted(names), batch_size=BATCH)
        self.counts["sites_restored"] = len(restored)
        # bulk_create не вызывает save(): домен уже нормализован при разборе,
        # строки «продукт × площадка» создаёт ensure_product_sites одним вызовом.
        created = Site.all_objects.bulk_create(new, batch_size=BATCH)
        ensure_product_sites(site_ids=[site.pk for site in created])
        for site in created:
            self.site_ids[site.domain] = site.pk
            self.created.add(site.domain)
        self.counts["sites_created"] = len(created)

    def _update_sites(self) -> None:
        """Факты карточки из каталога — перезаписываются; тип ссылки из прайса — заполняется."""
        records = {
            item.record.domain: item.record
            for item in self.plan.items
            if item.site is not None
            and (item.record.card is not None or item.record.link_type is not None)
        }
        if not records:
            return
        changed: list[Site] = []
        fields: set[str] = set()
        ids = [self.site_ids[domain] for domain in records]
        for start in range(0, len(ids), BATCH):
            for site in Site.all_objects.filter(pk__in=ids[start : start + BATCH]):
                record = records[site.domain]
                updates: dict[str, object]
                if record.card is not None:
                    updates = {k: v for k, v in record.card.items() if v is not None}
                elif not site.link_type:
                    updates = {"link_type": record.link_type}
                else:
                    updates = {}
                diff = [k for k, v in updates.items() if getattr(site, k) != v]
                for name in diff:
                    self.facts_changed_total += 1
                    if len(self.facts_changed) < LIST_LIMIT:
                        self.facts_changed.append(
                            f"{site.domain}: «{_label(name)}» {_show(getattr(site, name))} → "
                            f"{_show(updates[name])}"
                        )
                    setattr(site, name, updates[name])
                if diff:
                    site.updated_at = self.now
                    fields.update(diff)
                    changed.append(site)
        if changed:
            Site.all_objects.bulk_update(changed, [*sorted(fields), "updated_at"], batch_size=BATCH)
        self.counts["sites_updated"] = len(changed)

    # --- Предложения и рабочая цена ---

    def _write_offers(self) -> list[SitePrice]:
        """Предложение каждой строки плана; порядок результата — порядок `plan.items`."""
        ids = list({self.site_ids[item.record.domain] for item in self.plan.items})
        today: dict[tuple[int, str], SitePrice] = {}
        for start in range(0, len(ids), BATCH):
            rows = SitePrice.objects.filter(
                site_id__in=ids[start : start + BATCH],
                seller=self.seller,
                checked_at=self.checked_at,
            ).order_by("pk")
            for row in rows:
                today[(row.site_id, row.placement_type)] = row
        result: list[SitePrice] = []
        new: list[SitePrice] = []
        updated: list[SitePrice] = []
        for item in self.plan.items:
            site_id = self.site_ids[item.record.domain]
            values = self._price_values(item)
            reviewed = None if item.decision.needs_decision else self.now
            existing = today.get((site_id, item.service))
            working_id = item.site.working.id if item.site and item.site.working else None
            if existing is not None and existing.pk != working_id:
                # Снимок этой даты — обновляется на месте.
                for name, value in values.items():
                    setattr(existing, name, value)
                existing.reviewed_at = (existing.reviewed_at or self.now) if reviewed else None
                updated.append(existing)
                result.append(existing)
            elif existing is not None and all(getattr(existing, k) == v for k, v in values.items()):
                # Рабочая цена этой даты та же — повторная загрузка ничего не меняет.
                result.append(existing)
                self.counts["offers_unchanged"] += 1
            else:
                offer = SitePrice(
                    site_id=site_id,
                    seller=self.seller,
                    placement_type=item.service,
                    source=MetricSource.CSV_IMPORT,
                    checked_at=self.checked_at,
                    reviewed_at=reviewed,
                    **values,
                )
                new.append(offer)
                result.append(offer)
        SitePrice.objects.bulk_create(new, batch_size=BATCH)
        if updated:
            SitePrice.objects.bulk_update(updated, [*PRICE_FIELDS, "reviewed_at"], batch_size=BATCH)
        self.counts["offers_created"] = len(new)
        self.counts["offers_updated"] = len(updated)
        return result

    def _price_values(self, item: PlanItem) -> dict[str, object]:
        record = item.record
        return {
            "placement_cents": item.cents,
            "currency": self.plan.currency,
            **record.price_fields(),
            "extra": record.extra or None,
        }

    def _move_working(self, prices: Sequence[SitePrice]) -> None:
        moved: dict[int, int] = {}
        for item, price in zip(self.plan.items, prices, strict=True):
            if item.decision.becomes_working:
                moved[self.site_ids[item.record.domain]] = price.pk
        sites = []
        for site_id, price_id in moved.items():
            site = Site(pk=site_id, price_id=price_id, updated_at=self.now)
            sites.append(site)
        # bulk_update по готовым объектам: читать 45 000 площадок ради одного поля не нужно.
        Site.all_objects.bulk_update(sites, ["price", "updated_at"], batch_size=BATCH)
        self.counts["working_set"] = len(sites)

    def _supersede(self) -> None:
        """Старые неразобранные цены продавца за ту же услугу сменила новая — разобраны."""
        by_service: dict[str, list[int]] = {}
        for item in self.plan.items:
            by_service.setdefault(item.service, []).append(self.site_ids[item.record.domain])
        total = 0
        for service, ids in by_service.items():
            for start in range(0, len(ids), BATCH):
                total += SitePrice.objects.filter(
                    site_id__in=ids[start : start + BATCH],
                    seller=self.seller,
                    placement_type=service,
                    reviewed_at__isnull=True,
                    checked_at__lt=self.checked_at,
                ).update(reviewed_at=self.now)
        self.counts["offers_superseded"] = total

    # --- Метрики, заметки, список ---

    def _metrics(self) -> None:
        records = {
            item.record.domain: item.record
            for item in self.plan.items
            if any(item.record.metrics.get(name) is not None for name in METRIC_FIELDS)
        }
        ids = [self.site_ids[domain] for domain in records]
        existing: dict[int, SiteMetric] = {}
        for start in range(0, len(ids), BATCH):
            for metric in SiteMetric.objects.filter(
                site_id__in=ids[start : start + BATCH],
                source=MetricSource.CSV_IMPORT,
                seller=self.seller,
                checked_at=self.checked_at,
            ):
                existing[metric.site_id] = metric
        new: list[SiteMetric] = []
        updated: list[SiteMetric] = []
        for domain, record in records.items():
            site_id = self.site_ids[domain]
            values = {name: record.metrics.get(name) for name in METRIC_FIELDS}
            row = existing.get(site_id)
            if row is None:
                new.append(
                    SiteMetric(
                        site_id=site_id,
                        seller=self.seller,
                        source=MetricSource.CSV_IMPORT,
                        checked_at=self.checked_at,
                        **values,
                    )
                )
            elif any(getattr(row, k) != v for k, v in values.items()):
                for name, value in values.items():
                    setattr(row, name, value)
                updated.append(row)
        SiteMetric.objects.bulk_create(new, batch_size=BATCH)
        SiteMetric.objects.bulk_update(updated, list(METRIC_FIELDS), batch_size=BATCH)
        self.counts["metrics_created"] = len(new)
        self.counts["metrics_updated"] = len(updated)

    def _notes(self) -> None:
        notes = {
            item.record.domain: item.record.note for item in self.plan.items if item.record.note
        }
        if not notes:
            return
        ids = [self.site_ids[domain] for domain in notes]
        known = set(SiteNote.objects.filter(site_id__in=ids).values_list("site_id", "body"))
        new = [
            SiteNote(site_id=self.site_ids[domain], body=body, source=self.upload.file_name)
            for domain, body in notes.items()
            if (self.site_ids[domain], body) not in known
        ]
        SiteNote.objects.bulk_create(new)
        self.counts["notes_created"] = len(new)

    def _site_list(self) -> SiteList:
        site_list, _ = SiteList.objects.get_or_create(
            name=list_name(self.seller, self.upload.prices_date),
            defaults={"source": self.upload.file_name},
        )
        domains = {item.record.domain for item in self.plan.items}
        ids = [self.site_ids[domain] for domain in domains]
        present = set(
            SiteListItem.objects.filter(site_list=site_list).values_list("site_id", flat=True)
        )
        new = [
            SiteListItem(
                site_list=site_list, site_id=self.site_ids[d], first_seen=d in self.created
            )
            for d in domains
            if self.site_ids[d] not in present
        ]
        SiteListItem.objects.bulk_create(new, batch_size=BATCH)
        self.counts["list_items_created"] = len(new)
        self.counts["list_sites"] = len(ids)
        return site_list

    def _items(self, prices: Sequence[SitePrice]) -> None:
        items = []
        for item, price in zip(self.plan.items, prices, strict=True):
            working = item.site.working if item.site else None
            items.append(
                UploadItem(
                    upload=self.upload,
                    site_id=self.site_ids[item.record.domain],
                    price_id=price.pk,
                    ref_price_id=working.id if working else None,
                    review_group=item.decision.group,
                    needs_decision=item.decision.needs_decision,
                    auto_applied=item.decision.becomes_working,
                    site_created=item.record.domain in self.created,
                    line=item.record.line,
                    source_value=item.record.source_value,
                )
            )
        UploadItem.objects.bulk_create(items, batch_size=BATCH)
        self.counts["items"] = len(items)
        self.counts["needs_decision"] = sum(item.needs_decision for item in items)


def _label(name: str) -> str:
    field = Site._meta.get_field(name)
    return str(getattr(field, "verbose_name", name))


def _show(value: object) -> str:
    if value is None or value == "":
        return "пусто"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
