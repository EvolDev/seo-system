"""Запись разобранной таблицы в базу по правилам ADR-033 (маппинг §0–2).

Главнее админка. По видам данных:
- факты каталога — карточка Collaborator, объёмы ключей — перезаписываются
  непустыми значениями из таблицы; пустая ячейка значение в базе не стирает;
- замеры — метрики, цены, позиции — снапшот на дату файла: та же дата
  обновляется, новая — новая строка;
- решения и работа — статус по продукту, причина отказа, заметки,
  размещения, ссылки — только дополняются: недостающее создаётся, пустое
  заполняется, статусы двигаются вперёд. Расхождение — в отчёт.

Всё это выполняется внутри одной транзакции, её открывает `run.py`.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from django.db import models
from django.db.models import Count, Q
from django.utils import timezone

from apps.keywords.models import Keyword, KeywordPosition
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.importing import values
from apps.sites.importing.report import Outcome, Report, Section
from apps.sites.importing.rows import CopyRow, KeywordData, Link, PlacementData, SiteData
from apps.sites.importing.workbook import ImportAbort
from apps.sites.models import (
    MetricSource,
    Product,
    ProductSite,
    Site,
    SiteList,
    SiteListItem,
    SiteMetric,
    SitePrice,
    SiteStatus,
    ensure_product_sites,
)

CONVERTIO_DOMAIN = "convertio.co"
CLIDEO_DOMAIN = "clideo.com"
# Позиции во вкладке анкоров сняты по США (маппинг §2.2).
POSITIONS_COUNTRY = "US"

# Статус площадки по продукту импорт меняет, только если он «Новая» или
# вперёд по этой цепочке. Отклонённую, чёрный список, аудит не трогает.
SITE_LADDER = (SiteStatus.NEW, SiteStatus.APPROVED, SiteStatus.PLACED)
# Статус размещения — только вперёд. Отклонённое и отменённое не трогает.
PLACEMENT_LADDER = (
    PlacementStatus.PLANNED,
    PlacementStatus.ORDERED,
    PlacementStatus.WRITING,
    PlacementStatus.REVIEW,
    PlacementStatus.PUBLISHED,
)
WAITING = PLACEMENT_LADDER[:-1]


def start_of_day(day: dt.date) -> dt.datetime:
    """Начало дня по времени проекта: дата из таблицы → момент для базы."""
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


def find_product(domain: str) -> Product:
    product = Product.objects.filter(domain=domain).first()
    if product is None:
        raise ImportAbort(
            f"Нет продукта с доменом {domain}. Заведите продукты Convertio и Clideo "
            "в админке до импорта."
        )
    return product


def _ahead(current: str, target: str, ladder: Sequence[str]) -> bool:
    """`target` дальше `current` по цепочке статусов."""
    return current in ladder and target in ladder and ladder.index(target) > ladder.index(current)


def _label(obj: models.Model, field: str) -> str:
    model_field = obj._meta.get_field(field)
    return str(getattr(model_field, "verbose_name", field))


def _show(value: object) -> str:
    """Значение для отчёта: как его назвал бы человек."""
    if value is None or value == "":
        return "пусто"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, dt.datetime):
        return f"{timezone.localtime(value):%d.%m.%Y}"
    if isinstance(value, dt.date):
        return f"{value:%d.%m.%Y}"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


class Importer:
    """Один проход импорта: ключи, затем площадки и всё, что к ним относится."""

    def __init__(self, *, as_of: dt.date, list_name: str, source: str, report: Report) -> None:
        # Продукты — до любой записи: нет продукта, нет и импорта.
        self.convertio = find_product(CONVERTIO_DOMAIN)
        self.clideo = find_product(CLIDEO_DOMAIN)
        self.as_of = as_of
        self.checked_at = start_of_day(as_of)
        self.report = report
        self.site_list, created = SiteList.objects.get_or_create(
            name=list_name, defaults={"source": source}
        )
        report.note(f"Список «{list_name}»: {'создан' if created else 'уже был, дополняется'}")

    # --- Ключи и позиции (маппинг §2) ---

    def import_keywords(self, rows: Iterable[KeywordData]) -> dict[str, Keyword]:
        keywords = {k.keyword: k for k in Keyword.objects.filter(product=self.convertio)}
        for data in rows:
            where = f"ключ «{data.keyword}» (строка {data.row})"
            keyword = keywords.get(data.keyword)
            if keyword is None:
                if data.target_url is None:
                    self.report.issue(Section.SKIPPED_ROWS, f"{where}: нет URL — ключ не создан")
                    continue
                keyword = Keyword.objects.create(
                    product=self.convertio,
                    keyword=data.keyword,
                    target_url=data.target_url,
                    volume=data.volume,
                    global_volume=data.global_volume,
                    tool=data.tool,
                    page_type=data.page_type,
                )
                keywords[data.keyword] = keyword
                self.report.count("keywords", Outcome.CREATED)
            else:
                changed: list[str] = []
                # Объёмы — факт Ahrefs: перезаписываются.
                for field in ("volume", "global_volume"):
                    self._overwrite(keyword, field, getattr(data, field), where, changed)
                # Адрес, раздел, тип страницы — классификация человека: только заполняются.
                for field in ("target_url", "tool", "page_type"):
                    self._fill(
                        keyword,
                        field,
                        getattr(data, field),
                        where,
                        Section.KEYWORD_CONFLICTS,
                        changed,
                    )
                self._save(keyword, changed, "keywords")
            self._import_positions(keyword, data, where)
        return keywords

    def _import_positions(self, keyword: Keyword, data: KeywordData, where: str) -> None:
        existing = {
            position.checked_at: position
            for position in KeywordPosition.objects.filter(
                keyword=keyword, country=POSITIONS_COUNTRY
            )
        }
        for day, position in data.positions.items():
            current = existing.get(day)
            if current is None:
                KeywordPosition.objects.create(
                    keyword=keyword,
                    position=position,
                    country=POSITIONS_COUNTRY,
                    source=MetricSource.CSV_IMPORT,
                    checked_at=day,
                )
                self.report.count("keyword_positions", Outcome.CREATED)
            elif current.position == position:
                self.report.count("keyword_positions", Outcome.UNCHANGED)
            elif current.source == MetricSource.CSV_IMPORT:
                # Тот же замер из той же таблицы — исправление, а не новый снапшот.
                current.position = position
                current.save(update_fields=["position"])
                self.report.count("keyword_positions", Outcome.UPDATED)
            else:
                self.report.issue(
                    Section.KEYWORD_CONFLICTS,
                    f"{where}: позиция на {day:%d.%m.%Y} в базе {_show(current.position)} "
                    f"({current.get_source_display()}), в таблице {_show(position)}",
                )

    # --- Площадки (маппинг §1) ---

    def import_sites(self, rows: Sequence[SiteData], keywords: dict[str, Keyword]) -> None:
        self._report_new_categories(rows)
        sites, new_domains = self._create_sites(rows)
        site_ids = [site.pk for site in sites.values()]
        products = (self.convertio.pk, self.clideo.pk)
        product_sites = {
            (row.site_id, row.product_id): row
            for row in ProductSite.objects.filter(site_id__in=site_ids, product_id__in=products)
        }
        metrics = {
            row.site_id: row
            for row in SiteMetric.objects.filter(
                site_id__in=site_ids, source=MetricSource.CSV_IMPORT, checked_at=self.checked_at
            )
        }
        prices = {
            row.site_id: row
            for row in SitePrice.objects.filter(
                site_id__in=site_ids, source=MetricSource.CSV_IMPORT, checked_at=self.checked_at
            )
        }
        placements: dict[tuple[int, int], list[Placement]] = defaultdict(list)
        # prefetch_related загружает ссылки всех размещений одним запросом,
        # а не запросом на каждое размещение.
        for placement in Placement.objects.filter(
            site_id__in=site_ids, product_id__in=products
        ).prefetch_related("links"):
            placements[(placement.site_id, placement.product_id)].append(placement)
        in_list = set(
            SiteListItem.objects.filter(site_list=self.site_list).values_list("site_id", flat=True)
        )

        new_snapshots: list[models.Model] = []
        new_items: list[SiteListItem] = []
        undecided: list[int] = []
        for data in rows:
            site = sites.get(data.domain)
            if site is None:
                continue
            is_new = data.domain in new_domains
            if not is_new:
                self._update_site(site, data)
            self._snapshot(
                SiteMetric,
                metrics.get(site.pk),
                site,
                data.metrics.as_fields(),
                "site_metrics",
                new_snapshots,
            )
            self._snapshot(
                SitePrice,
                prices.get(site.pk),
                site,
                data.prices.as_fields(),
                "site_prices",
                new_snapshots,
            )
            self._import_placement(site, data, placements[(site.pk, self.convertio.pk)], keywords)
            convertio_row = product_sites[(site.pk, self.convertio.pk)]
            status, reason = self._convertio_decision(data)
            if status is None:
                if is_new:
                    undecided.append(convertio_row.pk)
            else:
                self._decide(convertio_row, status, reason, f"{data.where}, Convertio")
            self._import_clideo(
                site,
                data,
                placements[(site.pk, self.clideo.pk)],
                product_sites[(site.pk, self.clideo.pk)],
            )
            if site.pk in in_list:
                self.report.count("site_list_items", Outcome.UNCHANGED)
            else:
                new_items.append(
                    SiteListItem(site_list=self.site_list, site=site, first_seen=is_new)
                )
                self.report.count("site_list_items", Outcome.CREATED)

        # Новые строки — пачками: один INSERT на таблицу вместо двух тысяч.
        SiteMetric.objects.bulk_create(s for s in new_snapshots if isinstance(s, SiteMetric))
        SitePrice.objects.bulk_create(s for s in new_snapshots if isinstance(s, SitePrice))
        SiteListItem.objects.bulk_create(new_items)
        if undecided:
            ProductSite.objects.filter(pk__in=undecided).update(imported_undecided=True)
            self.report.count("product_sites", Outcome.UPDATED, len(undecided))

    def _create_sites(self, rows: Sequence[SiteData]) -> tuple[dict[str, Site], set[str]]:
        known = {
            site.domain: site
            for site in Site.all_objects.filter(domain__in=[row.domain for row in rows])
        }
        new_sites = []
        for data in rows:
            site = known.get(data.domain)
            if site is not None and site.is_deleted:
                self.report.issue(
                    Section.SKIPPED_ROWS, f"{data.where}: площадка помечена удалённой — пропущена"
                )
                del known[data.domain]
            elif site is None:
                note, _ = self._note_or_rejection(data)
                new_sites.append(Site(domain=data.domain, notes=note, **data.card.as_fields()))
        # bulk_create не вызывает save(): домен уже нормализован при разборе,
        # строки площадки под продукты создаём ниже одним вызовом.
        created = Site.all_objects.bulk_create(new_sites)
        ensure_product_sites(site_ids=[site.pk for site in created])
        self.report.count("sites", Outcome.CREATED, len(created))
        known.update((site.domain, site) for site in created)
        return known, {site.domain for site in created}

    def _update_site(self, site: Site, data: SiteData) -> None:
        changed: list[str] = []
        for field, value in data.card.as_fields().items():
            self._overwrite(site, field, value, data.where, changed)
        note, _ = self._note_or_rejection(data)
        self._fill(site, "notes", note, data.where, Section.DECISION_CONFLICTS, changed)
        self._save(site, changed, "sites")

    def _report_new_categories(self, rows: Sequence[SiteData]) -> None:
        known: set[str] = set()
        for topics, declared in Site.all_objects.values_list("topics", "declared_topics"):
            known.update(topics or [])
            known.update(declared or [])
        found: dict[tuple[str, str], int] = defaultdict(int)
        for data in rows:
            for kind, categories in (
                ("тематика", data.card.topics),
                ("особая тематика", data.card.declared_topics),
            ):
                for category in categories or []:
                    if category not in known:
                        found[(kind, category)] += 1
        for (kind, category), amount in sorted(found.items()):
            self.report.issue(Section.NEW_CATEGORIES, f"{kind} «{category}» — {amount}")

    def _snapshot(
        self,
        model: type[SiteMetric] | type[SitePrice],
        existing: SiteMetric | SitePrice | None,
        site: Site,
        fields: dict[str, object],
        table: str,
        pending: list[models.Model],
    ) -> None:
        if all(value is None for value in fields.values()):
            self.report.count(table, Outcome.SKIPPED)
            return
        if existing is None:
            pending.append(
                model(
                    site=site, source=MetricSource.CSV_IMPORT, checked_at=self.checked_at, **fields
                )
            )
            self.report.count(table, Outcome.CREATED)
            return
        changed = [field for field, value in fields.items() if getattr(existing, field) != value]
        for field in changed:
            setattr(existing, field, fields[field])
        self._save(existing, changed, table)

    # --- Решения по площадке (маппинг §1.2а, §1.6) ---

    @staticmethod
    def _note_or_rejection(data: SiteData) -> tuple[str | None, str | None]:
        """«Комментарий к площадке»: (заметка, причина отказа) — одно из двух."""
        if values.is_rejection(data.comment):
            return None, data.comment
        return data.comment, None

    def _convertio_decision(self, data: SiteData) -> tuple[SiteStatus | None, str | None]:
        _, reason = self._note_or_rejection(data)
        if data.placement is not None:
            if reason:
                self.report.issue(
                    Section.DECISION_CONFLICTS,
                    f"{data.where}: в таблице и размещение, и отказ «{reason}» — "
                    "применено размещение",
                )
            if data.placement.status == PlacementStatus.PUBLISHED:
                return SiteStatus.PLACED, None
            return SiteStatus.APPROVED, None
        if reason:
            return SiteStatus.REJECTED, reason
        return None, None

    def _decide(self, row: ProductSite, status: SiteStatus, reason: str | None, where: str) -> None:
        changed: list[str] = []
        current = row.status
        if current == status:
            if reason:
                self._fill(row, "reject_reason", reason, where, Section.DECISION_CONFLICTS, changed)
        elif current == SiteStatus.NEW or _ahead(current, status, SITE_LADDER):
            row.status = status
            changed.append("status")
            if reason:
                row.reject_reason = reason
                changed.append("reject_reason")
        elif not _ahead(status, current, SITE_LADDER):
            # База не просто впереди по цепочке, а решила иначе: не трогаем.
            self.report.issue(
                Section.DECISION_CONFLICTS,
                f"{where}: в таблице «{status.label}», в базе "
                f"«{row.get_status_display()}» — не тронуто",
            )
        self._save(row, changed, "product_sites")

    # --- Размещения Convertio (маппинг §1.5) ---

    def _import_placement(
        self,
        site: Site,
        data: SiteData,
        candidates: list[Placement],
        keywords: dict[str, Keyword],
    ) -> None:
        wanted = data.placement
        if wanted is None:
            return
        placement, ambiguous = _match_placement(candidates, wanted.article_url)
        if ambiguous:
            self.report.issue(
                Section.PLACEMENT_CONFLICTS,
                f"{data.where}: у площадки несколько размещений Convertio без адреса "
                "статьи — какое обновлять, неясно, не тронуто",
            )
            return
        existing_links: dict[int | None, PlacementLink] = {}
        if placement is None:
            placement = Placement.objects.create(
                site=site,
                product=self.convertio,
                status=wanted.status,
                article_url=wanted.article_url,
                published_at=start_of_day(wanted.published_on) if wanted.published_on else None,
                is_indexed=wanted.is_indexed,
                placement_type=wanted.placement_type,
                comment=wanted.comment,
            )
            candidates.append(placement)
            self.report.count("placements", Outcome.CREATED)
        else:
            self._update_placement(placement, wanted, data.where)
            existing_links = {link.link_index: link for link in placement.links.all()}
        for link in wanted.links:
            self._import_link(placement, link, existing_links.get(link.index), keywords, data.where)

    def _update_placement(self, placement: Placement, wanted: PlacementData, where: str) -> None:
        changed: list[str] = []
        current = placement.status
        if _ahead(current, wanted.status, PLACEMENT_LADDER):
            placement.status = wanted.status
            changed.append("status")
        elif current == PlacementStatus.PUBLISHED and wanted.status != current:
            self.report.issue(
                Section.PLACEMENT_CONFLICTS,
                f"{where}: в базе «Опубликовано», в таблице «{wanted.status.label}» — не тронуто",
            )
        # Отклонённое и отменённое таблица выразить не может — молчим.
        # «Пишется» и «На проверке» — тоже: база впереди, это нормально.
        self._fill(
            placement,
            "article_url",
            wanted.article_url,
            where,
            Section.PLACEMENT_CONFLICTS,
            changed,
        )
        if wanted.published_on is not None:
            if placement.published_at is None:
                placement.published_at = start_of_day(wanted.published_on)
                changed.append("published_at")
            elif timezone.localtime(placement.published_at).date() != wanted.published_on:
                self.report.issue(
                    Section.PLACEMENT_CONFLICTS,
                    f"{where}: «{_label(placement, 'published_at')}» в базе "
                    f"{_show(placement.published_at)}, в таблице {_show(wanted.published_on)}",
                )
        for field in ("is_indexed", "placement_type", "comment"):
            self._fill(
                placement,
                field,
                getattr(wanted, field),
                where,
                Section.PLACEMENT_CONFLICTS,
                changed,
            )
        self._save(placement, changed, "placements")

    def _import_link(
        self,
        placement: Placement,
        link: Link,
        existing: PlacementLink | None,
        keywords: dict[str, Keyword],
        where: str,
    ) -> None:
        keyword = keywords.get(link.anchor)
        if keyword is None:
            self.report.issue(
                Section.ANCHOR_NO_KEYWORD, f"{where}: «{link.anchor}» → {link.target_url}"
            )
        elif keyword.target_url.rstrip("/") != link.target_url.rstrip("/"):
            self.report.issue(
                Section.ANCHOR_OTHER_URL,
                f"{where}: «{link.anchor}» ведёт на {link.target_url}, "
                f"у ключа — {keyword.target_url}",
            )
        if existing is None:
            PlacementLink.objects.create(
                placement=placement,
                keyword=keyword,
                anchor=link.anchor,
                target_url=link.target_url,
                link_index=link.index,
            )
            self.report.count("placement_links", Outcome.CREATED)
            return
        changed: list[str] = []
        # Ключ дописываем, только если анкор тот же: иначе ключ нашёлся по
        # анкору из таблицы, а в базе у ссылки анкор другой.
        if existing.keyword_id is None and keyword is not None and existing.anchor == link.anchor:
            existing.keyword = keyword
            changed.append("keyword")
        for field in ("anchor", "target_url"):
            if getattr(existing, field) != getattr(link, field):
                self.report.issue(
                    Section.PLACEMENT_CONFLICTS,
                    f"{where}: ссылка {link.index}, «{_label(existing, field)}» в базе "
                    f"{getattr(existing, field)!r}, в таблице {getattr(link, field)!r}",
                )
        self._save(existing, changed, "placement_links")

    # --- Размещения Clideo (маппинг §1.6) ---

    def _import_clideo(
        self,
        site: Site,
        data: SiteData,
        candidates: list[Placement],
        product_site: ProductSite,
    ) -> None:
        url = data.clideo_url
        if url is None:
            return
        if not values.is_url_on_domain(url, site.domain):
            self.report.issue(Section.CLIDEO_BAD, f"{data.where}: {url}")
            return
        if any(placement.article_url == url for placement in candidates):
            self.report.count("placements", Outcome.UNCHANGED)
        else:
            candidates.append(
                Placement.objects.create(
                    site=site,
                    product=self.clideo,
                    status=PlacementStatus.PUBLISHED,
                    article_url=url,
                )
            )
            self.report.count("placements", Outcome.CREATED)
        self._decide(product_site, SiteStatus.PLACED, None, f"{data.where}, Clideo")

    # --- Сверки после записи ---

    def compare_link_counts(
        self, rows: Iterable[KeywordData], keywords: dict[str, Keyword]
    ) -> None:
        """Links Placed / Waiting из таблицы против расчёта по базе (маппинг §2.1)."""
        counted = {
            keyword.pk: keyword
            for keyword in Keyword.objects.filter(product=self.convertio).annotate(
                placed=Count("links", filter=Q(links__placement__status=PlacementStatus.PUBLISHED)),
                waiting=Count("links", filter=Q(links__placement__status__in=WAITING)),
            )
        }
        for data in rows:
            keyword = keywords.get(data.keyword)
            if keyword is None:
                continue
            row: Any = counted[keyword.pk]
            in_file = (data.links_placed or 0, data.links_waiting or 0)
            in_base = (row.placed, row.waiting)
            if in_file != in_base:
                self.report.issue(
                    Section.LINK_COUNTS,
                    f"«{data.keyword}» (строка {data.row}): в таблице {in_file[0]} / "
                    f"{in_file[1]}, по базе {in_base[0]} / {in_base[1]}",
                )

    # --- Общие шаги ---

    def _overwrite(
        self, obj: models.Model, field: str, value: object, where: str, changed: list[str]
    ) -> None:
        """Факт из внешнего источника: непустое значение из таблицы заменяет базу."""
        if value is None:
            return
        old = getattr(obj, field)
        if old == value:
            return
        setattr(obj, field, value)
        changed.append(field)
        self.report.issue(
            Section.CATALOG_CHANGED,
            f"{where}: «{_label(obj, field)}» {_show(old)} → {_show(value)}",
        )

    def _fill(
        self,
        obj: models.Model,
        field: str,
        value: object,
        where: str,
        section: Section,
        changed: list[str],
    ) -> None:
        """Решение или работа человека: пустое заполняется, заполненное не трогается."""
        if value is None:
            return
        old = getattr(obj, field)
        if old is None or old == "":
            setattr(obj, field, value)
            changed.append(field)
        elif old != value:
            self.report.issue(
                section,
                f"{where}: «{_label(obj, field)}» в базе {_show(old)}, в таблице {_show(value)}",
            )

    def _save(self, obj: models.Model, changed: list[str], table: str) -> None:
        if changed:
            obj.save(update_fields=changed)
            self.report.count(table, Outcome.UPDATED)
        else:
            self.report.count(table, Outcome.UNCHANGED)


def _match_placement(
    candidates: list[Placement], article_url: str | None
) -> tuple[Placement | None, bool]:
    """Какое размещение обновлять (маппинг §1.5): (размещение или None, неоднозначно ли).

    Сначала — с тем же адресом статьи; иначе — единственное без адреса;
    нет подходящих — создаётся новое.
    """
    if article_url:
        same_url = [p for p in candidates if p.article_url == article_url]
        if same_url:
            return same_url[0], False
    without_url = [p for p in candidates if not p.article_url]
    if len(without_url) > 1:
        return None, True
    if without_url:
        return without_url[0], False
    return None, False


def compare_copy(copy_rows: Iterable[CopyRow], sites: Iterable[SiteData], report: Report) -> None:
    """Сверка вкладки «Размещения» с основной (маппинг §1.8). В базу не пишет."""
    by_domain = {site.domain: site for site in sites}
    columns = (
        ("status", "Статус"),
        ("article_url", "URL статьи"),
        ("published_on", "Дата размещения"),
        ("is_indexed", "Индексация"),
        ("placement_type", "Тип ссылки"),
    )
    seen: set[str] = set()
    for copy in copy_rows:
        seen.add(copy.domain)
        site = by_domain.get(copy.domain)
        if site is None:
            report.issue(
                Section.COPY_MISMATCH,
                f"{copy.domain} (строка {copy.row}): есть в «Размещениях», нет в основной",
            )
            continue
        base = site.placement_cells
        for field, column in columns:
            in_base, in_copy = getattr(base, field), getattr(copy, field)
            if in_base != in_copy:
                report.issue(
                    Section.COPY_MISMATCH,
                    f"{copy.domain}: «{column}» в основной {_show(in_base)}, "
                    f"в «Размещениях» {_show(in_copy)}",
                )
    for site in by_domain.values():
        if site.placement_cells.status == "Размещено" and site.domain not in seen:
            report.issue(
                Section.COPY_MISMATCH,
                f"{site.where}: размещена в основной вкладке, в «Размещениях» её нет",
            )
