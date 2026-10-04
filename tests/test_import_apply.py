"""Запись таблицы в базу по правилам ADR-033 (E1-04, маппинг §0–3).

Книгу тест собирает сам (`make_workbook` в conftest). Строки основной
вкладки — `("домен", {колонка: значение})` поверх типовой строки.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.keywords.models import Keyword, KeywordPosition
from apps.observability.models import TaskRun, TaskStatus
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.importing.report import Outcome, Report, Section
from apps.sites.importing.run import ImportOptions, run_import
from apps.sites.importing.workbook import ImportAbort
from apps.sites.models import (
    PlacementType,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteAudit,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
)
from apps.sites.offers import TABLE_SOURCE

pytestmark = pytest.mark.django_db

MakeWorkbook = Callable[..., Path]
AS_OF = dt.date(2026, 9, 27)
SEPTEMBER = "Сентябрь 2026"

PUBLISHED = {
    "Статус": "Размещено",
    "URL статьи": "https://a.com/post",
    "Индексация": "Да",
    "Дата размещения": "14.09.2026",
    "Тип ссылки": "Guest Post",
    "Анкор1": "mp4 to mp3",
    "Ссылка1": "https://convertio.co/mp4-mp3/",
    "Анкор2": "Convertio",
    "Ссылка2": "https://convertio.co/",
}
ORDERED = {"Статус": "Заявка отправлена", "Анкор1": "convert", "Ссылка1": "https://convertio.co/"}


@pytest.fixture
def products() -> tuple[Product, Product]:
    return (
        Product.objects.create(name="Convertio", domain="convertio.co"),
        Product.objects.create(name="Clideo", domain="clideo.com"),
    )


@pytest.fixture
def run(tmp_path: Path) -> Callable[..., Report]:
    def run_(
        path: Path, *, as_of: dt.date = AS_OF, list_name: str = SEPTEMBER, dry_run: bool = False
    ) -> Report:
        options = ImportOptions(path=path, as_of=as_of, list_name=list_name, dry_run=dry_run)
        return run_import(options, tmp_path / "reports").report

    return run_


KEYWORDS = [
    ("convert", {}),
    ("mp4 to mp3", {"URL": "https://convertio.co/mp4-mp3/", "Tool": "Audio"}),
]


def _status(domain: str, product: Product) -> ProductSite:
    return ProductSite.objects.get(site__domain=domain, product=product)


@pytest.fixture
def book(make_workbook: MakeWorkbook) -> Path:
    return make_workbook(
        base=[
            (
                "a.com",
                {
                    **PUBLISHED,
                    "Комментарий к площадке": "Делают инсёрт в существующую статью",
                    "Пример статьи на Clideo": "https://www.a.com/clideo-article",
                },
            ),
            ("b.com", ORDERED),
            (
                "c.com",
                {
                    "Анкор1": "convert",
                    "Ссылка1": "https://convertio.co/",
                    "Комментарий к площадке": "Отказались писать",
                },
            ),
            (
                "d.com",
                {
                    "Комментарий к площадке": "Nofollow, отбрасываем",
                    "Тип ссылки статья": "nofollow",
                },
            ),
            ("e.com", {}),
            ("f.com", {"Пометка о рекламе статья": "Да"}),
            ("g.com", {"Пример статьи на Clideo": "https://other.com/post"}),
        ],
        keywords=KEYWORDS,
    )


class TestFirstImport:
    def test_decisions_under_convertio(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        convertio, _ = products
        run(book)
        expected = {
            "a.com": SiteStatus.PLACED,
            "b.com": SiteStatus.ORDERED,
            "c.com": SiteStatus.APPROVED,
            "d.com": SiteStatus.DISCARDED,
            "e.com": SiteStatus.NEW,
            "f.com": SiteStatus.NEW,
            "g.com": SiteStatus.NEW,
        }
        rows = ProductSite.objects.filter(product=convertio).select_related("site")
        assert {row.site.domain: row.status for row in rows} == expected
        assert _status("d.com", convertio).reject_reason == "Nofollow, отбрасываем"
        undecided = set(rows.filter(imported_undecided=True).values_list("site__domain", flat=True))
        assert undecided == {"e.com", "f.com", "g.com"}

    def test_placements_and_links(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        convertio, _ = products
        report = run(book)
        placements = Placement.objects.filter(product=convertio).select_related("site")
        assert {p.site.domain: p.status for p in placements} == {
            "a.com": PlacementStatus.PUBLISHED,
            "b.com": PlacementStatus.ORDERED,
            "c.com": PlacementStatus.PLANNED,
        }
        published = placements.get(site__domain="a.com")
        assert published.article_url == "https://a.com/post"
        assert timezone.localtime(published.published_at).date() == dt.date(2026, 9, 14)
        assert published.is_indexed is True
        links = {link.link_index: link for link in published.links.select_related("keyword")}
        assert links[1].keyword is not None
        assert links[1].keyword.keyword == "mp4 to mp3"
        assert links[2].keyword is None
        assert report.issues[Section.ANCHOR_NO_KEYWORD] == [
            "a.com (строка 2): «Convertio» → https://convertio.co/"
        ]
        assert "Отказались писать" in report.issues[Section.REFUSALS][0]

    def test_clideo_examples(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        _, clideo = products
        report = run(book)
        [placement] = Placement.objects.filter(product=clideo)
        assert placement.site.domain == "a.com"
        assert placement.status == PlacementStatus.PUBLISHED
        assert PlacementLink.objects.filter(placement=placement).count() == 0
        assert _status("a.com", clideo).status == SiteStatus.PLACED
        assert _status("g.com", clideo).status == SiteStatus.NEW
        assert report.issues[Section.CLIDEO_BAD] == ["g.com (строка 8): https://other.com/post"]

    def test_ad_label_is_a_fact_not_a_stop(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        convertio, _ = products
        run(book)
        assert Site.objects.get(domain="f.com").marks_as_ad is True
        assert _status("f.com", convertio).status == SiteStatus.NEW
        assert SiteAudit.objects.count() == 0

    def test_facts_snapshots_list(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        run(book)
        site = Site.objects.get(domain="a.com")
        assert site.language == "en"
        metric = SiteMetric.objects.get(site=site)
        assert timezone.localtime(metric.checked_at) == timezone.make_aware(
            dt.datetime(2026, 9, 27)
        )
        assert metric.source == "csv_import"
        items = SiteListItem.objects.filter(site_list__name=SEPTEMBER)
        assert items.count() == 7
        assert all(item.first_seen for item in items)
        assert KeywordPosition.objects.filter(keyword__keyword="convert").count() == 4


class TestPricesAndNotes:
    """Цены — предложения Collaborator, комментарии — в истории заметок (ADR-043)."""

    def test_prices_are_collaborator_offers_and_working(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        run(book)
        site = Site.objects.get(domain="a.com")
        price = SitePrice.objects.get(site=site)
        assert price.seller == Seller.collaborator()
        assert price.placement_type == PlacementType.GUEST_POST
        assert (price.placement_cents, price.writing_cents) == (54457, 4084)
        # Первая цена — рабочая сама, решать по ней нечего.
        assert site.price_id == price.pk
        assert price.reviewed_at is not None

    def test_comments_go_to_history(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        convertio, _ = products
        report = run(book)
        notes = {
            (note.site.domain, note.product, note.body)
            for note in SiteNote.objects.select_related("site", "product")
        }
        assert ("a.com", None, "Делают инсёрт в существующую статью") in notes
        # Отказ — заметка под Convertio; причина остаётся и в решении по продукту.
        assert ("d.com", convertio, "Nofollow, отбрасываем") in notes
        assert all(note.source == TABLE_SOURCE for note in SiteNote.objects.all())
        assert report.counts["site_notes"][Outcome.CREATED] == len(notes)

    def test_second_run_adds_no_notes(
        self, book: Path, products: tuple[Product, Product], run: Callable[..., Report]
    ) -> None:
        run(book)
        notes = SiteNote.objects.count()
        run(book)
        assert SiteNote.objects.count() == notes

    def test_new_comment_is_new_note(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("a.com", {"Комментарий к площадке": "Пишут быстро"})]))
        run(
            make_workbook(
                base=[("a.com", {"Комментарий к площадке": "Подняли цену"})], name="2.xlsx"
            )
        )
        bodies = set(SiteNote.objects.values_list("body", flat=True))
        assert bodies == {"Пишут быстро", "Подняли цену"}

    def test_working_price_is_not_changed_by_import(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        price = "Цена размещения статья, EUR"
        run(make_workbook(base=[("a.com", {price: 200})], name="1.xlsx"))
        site = Site.objects.get(domain="a.com")
        working = site.price_id
        # Та же дата, другая цена: рабочая не переписывается — рядом новое предложение.
        run(make_workbook(base=[("a.com", {price: 220})], name="2.xlsx"))
        # Новая дата — тоже новое предложение, ждёт решения.
        run(
            make_workbook(base=[("a.com", {price: 230})], name="3.xlsx"),
            as_of=dt.date(2026, 10, 27),
        )
        site.refresh_from_db()
        assert site.price_id == working
        offers = SitePrice.objects.filter(site=site).order_by("pk")
        assert [o.placement_cents for o in offers] == [20000, 22000, 23000]
        assert [o.reviewed_at is None for o in offers] == [False, True, True]

    def test_same_price_on_new_date_needs_no_decision(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        book = make_workbook(base=[("a.com", {})])
        run(book)
        run(book, as_of=dt.date(2026, 10, 27))
        assert not SitePrice.objects.filter(reviewed_at__isnull=True).exists()


class TestRepeatedImport:
    def test_second_run_creates_nothing(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        book = make_workbook(
            base=[("a.com", {**PUBLISHED, "Пример статьи на Clideo": "https://a.com/c"})],
            keywords=KEYWORDS,
        )
        run(book)
        totals = (
            Site.objects.count(),
            Placement.objects.count(),
            PlacementLink.objects.count(),
            SiteMetric.objects.count(),
            KeywordPosition.objects.count(),
            SiteListItem.objects.count(),
        )
        report = run(book)
        assert all(Outcome.CREATED not in counter for counter in report.counts.values())
        assert totals == (
            Site.objects.count(),
            Placement.objects.count(),
            PlacementLink.objects.count(),
            SiteMetric.objects.count(),
            KeywordPosition.objects.count(),
            SiteListItem.objects.count(),
        )

    def test_admin_decision_is_kept(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        convertio, _ = products
        book = make_workbook(base=[("b.com", ORDERED)], keywords=KEYWORDS)
        run(book)
        row = _status("b.com", convertio)
        row.status = SiteStatus.DECLINED
        row.reject_reason = "отказали в админке"
        row.save()
        report = run(book)
        row.refresh_from_db()
        assert row.status == SiteStatus.DECLINED
        assert report.issues[Section.DECISION_CONFLICTS] == [
            "b.com (строка 2), Convertio: в таблице «Заявка отправлена», "
            "в базе «Отказала площадка» — не тронуто"
        ]

    def test_new_order_overrides_earlier_refusal(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        # Отказ стоял до заявки: заявка, впервые пришедшая из таблицы, — новый
        # факт, как и заявка, заведённая в админке (ADR-047).
        convertio, _ = products
        Site.objects.create(domain="b.com")
        ProductSite.objects.filter(site__domain="b.com", product=convertio).update(
            status=SiteStatus.DISCARDED, reject_reason="дорого, отбрасываем"
        )
        report = run(make_workbook(base=[("b.com", ORDERED)], keywords=KEYWORDS))
        row = _status("b.com", convertio)
        assert (row.status, row.reject_reason) == (SiteStatus.ORDERED, "дорого, отбрасываем")
        assert Section.DECISION_CONFLICTS not in report.issues

    def test_blacklist_is_kept_with_new_order(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        convertio, _ = products
        Site.objects.create(domain="b.com")
        ProductSite.objects.filter(site__domain="b.com", product=convertio).update(
            status=SiteStatus.BLACKLISTED
        )
        report = run(make_workbook(base=[("b.com", ORDERED)], keywords=KEYWORDS))
        assert _status("b.com", convertio).status == SiteStatus.BLACKLISTED
        assert report.issues[Section.DECISION_CONFLICTS] == [
            "b.com (строка 2), Convertio: в таблице «Заявка отправлена», в базе «Чёрный список» "
            "— не тронуто"
        ]

    def test_clideo_placement_from_upload_is_completed(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        # Список доменов от продавца (E1-09) дал размещение Clideo без адреса;
        # таблица с адресом его дополняет, а не заводит второе (ADR-051).
        _, clideo = products
        site = Site.objects.create(domain="a.com")
        placement = Placement.objects.create(
            site=site, product=clideo, status=PlacementStatus.ORDERED
        )
        book = make_workbook(
            base=[("a.com", {"Пример статьи на Clideo": "https://a.com/clideo"})],
            keywords=KEYWORDS,
        )
        run(book)
        placement.refresh_from_db()
        assert Placement.objects.filter(product=clideo).count() == 1
        assert placement.article_url == "https://a.com/clideo"
        assert placement.status == PlacementStatus.PUBLISHED
        assert _status("a.com", clideo).status == SiteStatus.PLACED

    def test_clideo_article_matches_other_url_notation(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        # Адрес из файла коллеги — без www и метки текста: та же статья.
        _, clideo = products
        site = Site.objects.create(domain="a.com")
        Placement.objects.create(
            site=site,
            product=clideo,
            status=PlacementStatus.PUBLISHED,
            article_url="https://a.com/blog/clideo/",
        )
        url = "https://www.a.com/blog/clideo#:~:text=Video%20editor"
        report = run(
            make_workbook(base=[("a.com", {"Пример статьи на Clideo": url})], keywords=KEYWORDS)
        )
        [placement] = Placement.objects.filter(product=clideo)
        assert placement.article_url == "https://a.com/blog/clideo/"
        assert report.counts["placements"][Outcome.UNCHANGED] == 1

    def test_placement_moves_forward_only(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        convertio, _ = products
        run(make_workbook(base=[("a.com", ORDERED)], keywords=KEYWORDS, name="1.xlsx"))
        published = {
            **ORDERED,
            "Статус": "Размещено",
            "URL статьи": "https://a.com/post",
            "Дата размещения": "14.09.2026",
        }
        run(make_workbook(base=[("a.com", published)], keywords=KEYWORDS, name="2.xlsx"))
        [placement] = Placement.objects.filter(product=convertio)
        assert placement.status == PlacementStatus.PUBLISHED
        assert placement.article_url == "https://a.com/post"
        assert _status("a.com", convertio).status == SiteStatus.PLACED

        # Та же статья, а в таблице снова «Заявка» — назад не двигаем.
        back = {**published, "Статус": "Заявка отправлена"}
        report = run(make_workbook(base=[("a.com", back)], keywords=KEYWORDS, name="3.xlsx"))
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.PUBLISHED
        assert report.issues[Section.PLACEMENT_CONFLICTS] == [
            "a.com (строка 2): в базе «Опубликовано», в таблице «Заявка отправлена» — не тронуто"
        ]

    def test_new_order_on_placed_site_is_new_placement(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        # Строка площадки одна: новая заявка перезаписывает в таблице прежнюю
        # статью. Заявка без адреса — это новое размещение (маппинг §1.5).
        convertio, _ = products
        run(make_workbook(base=[("a.com", PUBLISHED)], keywords=KEYWORDS, name="1.xlsx"))
        run(make_workbook(base=[("a.com", ORDERED)], keywords=KEYWORDS, name="2.xlsx"))
        statuses = Placement.objects.filter(product=convertio).values_list("status", flat=True)
        assert sorted(statuses) == [PlacementStatus.ORDERED, PlacementStatus.PUBLISHED]
        assert _status("a.com", convertio).status == SiteStatus.PLACED

    def test_filled_placement_field_is_kept(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("a.com", PUBLISHED)], name="1.xlsx"))
        changed = {**PUBLISHED, "Индексация": "Нет"}
        report = run(make_workbook(base=[("a.com", changed)], name="2.xlsx"))
        assert Placement.objects.get(site__domain="a.com").is_indexed is True
        assert report.issues[Section.PLACEMENT_CONFLICTS] == [
            "a.com (строка 2): «в индексе» в базе да, в таблице нет"
        ]

    def test_snapshot_same_date_updated_new_date_added(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("a.com", {"DR": 40})], name="1.xlsx"))
        run(make_workbook(base=[("a.com", {"DR": 41})], name="2.xlsx"))
        assert list(SiteMetric.objects.values_list("dr", flat=True)) == [41]
        run(make_workbook(base=[("a.com", {"DR": 42})], name="3.xlsx"), as_of=dt.date(2026, 10, 27))
        assert set(SiteMetric.objects.values_list("dr", flat=True)) == {41, 42}

    def test_us_traffic_is_country_snapshot(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        # US Traff — трафик США у каждой площадки, не топ-регион (ADR-045).
        row = {"DR": 40, "Top Geo": "in", "Top Geo Traff": 900, "US Traff": 120}
        run(make_workbook(base=[("a.com", row)], name="1.xlsx"))
        us = SiteCountryMetric.objects.get(site__domain="a.com")
        assert (us.country, us.organic_traffic, us.total_keywords) == ("us", 120, None)
        assert us.source == "csv_import"
        metric = SiteMetric.objects.get(site__domain="a.com")
        assert us.checked_at == metric.checked_at
        assert (metric.top_geo, metric.top_geo_traffic) == ("in", 900)
        run(make_workbook(base=[("a.com", {**row, "US Traff": 130})], name="2.xlsx"))
        assert list(SiteCountryMetric.objects.values_list("organic_traffic", flat=True)) == [130]
        run(
            make_workbook(base=[("a.com", {**row, "US Traff": 140})], name="3.xlsx"),
            as_of=dt.date(2026, 10, 27),
        )
        assert set(SiteCountryMetric.objects.values_list("organic_traffic", flat=True)) == {
            130,
            140,
        }

    def test_no_us_traffic_no_country_snapshot(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        report = run(make_workbook(base=[("a.com", {"DR": 40, "US Traff": None})]))
        assert SiteCountryMetric.objects.count() == 0
        assert report.counts["site_country_metrics"][Outcome.SKIPPED] == 1

    def test_position_same_date_updated(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(keywords=[("convert", {})], name="1.xlsx"))
        run(make_workbook(keywords=[("convert", {"Pos 22.09.26": 101})], name="2.xlsx"))
        positions = KeywordPosition.objects.filter(keyword__keyword="convert")
        assert positions.count() == 4
        assert positions.get(checked_at=dt.date(2026, 9, 22)).position is None

    def test_catalog_overwritten_empty_cell_keeps_value(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        first = {"Особые тематики": "Азартные игры", "Количество ссылок статья": 2}
        run(make_workbook(base=[("a.com", first)], name="1.xlsx"))
        second = {"Особые тематики": "", "Количество ссылок статья": 3}
        report = run(make_workbook(base=[("a.com", second)], name="2.xlsx"))
        site = Site.objects.get(domain="a.com")
        assert site.links_allowed == 3
        assert site.declared_topics == ["Азартные игры"]
        assert report.issues[Section.CATALOG_CHANGED] == [
            "a.com (строка 2): «ссылок разрешено» 2 → 3"
        ]

    def test_keyword_volume_overwritten_tool_kept(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(keywords=[("convert", {"Volume": 42000})], name="1.xlsx"))
        second = {"Volume": 45000, "Tool": "Video"}
        report = run(make_workbook(keywords=[("convert", second)], name="2.xlsx"))
        keyword = Keyword.objects.get(keyword="convert")
        assert (keyword.volume, keyword.tool) == (45000, "Main")
        assert Section.KEYWORD_CONFLICTS in report.issues

    def test_link_gets_keyword_when_it_appears(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("b.com", ORDERED)], name="1.xlsx"))
        link = PlacementLink.objects.get()
        assert link.keyword is None
        run(make_workbook(base=[("b.com", ORDERED)], keywords=KEYWORDS, name="2.xlsx"))
        link.refresh_from_db()
        assert link.keyword is not None
        assert link.keyword.keyword == "convert"

    def test_link_with_other_anchor_is_not_bound(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("b.com", ORDERED)], name="1.xlsx"))
        PlacementLink.objects.update(anchor="convert files")
        report = run(make_workbook(base=[("b.com", ORDERED)], keywords=KEYWORDS, name="2.xlsx"))
        assert PlacementLink.objects.get().keyword is None
        assert report.issues[Section.PLACEMENT_CONFLICTS] == [
            "b.com (строка 2): ссылка 1, «анкор» в базе 'convert files', в таблице 'convert'"
        ]


class TestLists:
    def test_list_extended_and_new_list_keeps_history(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        run(make_workbook(base=[("a.com", {})], name="1.xlsx"))
        run(make_workbook(base=[("a.com", {}), ("h.com", {})], name="2.xlsx"))
        september = SiteListItem.objects.filter(site_list__name=SEPTEMBER)
        assert dict(september.values_list("site__domain", "first_seen")) == {
            "a.com": True,
            "h.com": True,
        }
        run(make_workbook(base=[("a.com", {})], name="3.xlsx"), list_name="Октябрь 2026")
        october = SiteListItem.objects.get(site_list__name="Октябрь 2026")
        assert (october.site.domain, october.first_seen) == ("a.com", False)
        assert SiteList.objects.count() == 2


class TestChecks:
    def test_link_counts_compared_with_file(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        book = make_workbook(
            base=[("b.com", ORDERED)],
            keywords=[("convert", {"Links Placed": 5, "Links Waiting": 2})],
        )
        report = run(book)
        assert report.issues[Section.LINK_COUNTS] == [
            "«convert» (строка 2): в таблице 5 / 2, по базе 0 / 1"
        ]

    def test_copy_sheet_compared(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        book = make_workbook(
            base=[("a.com", PUBLISHED), ("b.com", {**PUBLISHED, "URL статьи": "https://b.com/x"})],
            copy=[("a.com", {**PUBLISHED, "Статус": "404 Ошибка"})],
        )
        report = run(book)
        assert report.issues[Section.COPY_MISMATCH] == [
            "a.com: «Статус» в основной Размещено, в «Размещениях» 404 Ошибка",
            "b.com (строка 3): размещена в основной вкладке, в «Размещениях» её нет",
        ]

    def test_deleted_site_is_skipped(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        Site.objects.create(domain="a.com", is_deleted=True)
        report = run(make_workbook(base=[("a.com", {"DR": 90})]))
        assert SiteMetric.objects.count() == 0
        assert "помечена удалённой" in report.issues[Section.SKIPPED_ROWS][0]


class TestRunAndCommand:
    def test_without_products_nothing_is_written(
        self, make_workbook: MakeWorkbook, run: Callable[..., Report]
    ) -> None:
        with pytest.raises(ImportAbort, match=r"convertio\.co"):
            run(make_workbook(base=[("a.com", {})], keywords=KEYWORDS))
        assert Site.all_objects.count() == 0
        assert Keyword.objects.count() == 0
        task = TaskRun.objects.get()
        assert task.status == TaskStatus.FAILED
        assert "convertio.co" in (task.error or "")

    def test_dry_run_same_report_nothing_written(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        run: Callable[..., Report],
    ) -> None:
        book = make_workbook(base=[("a.com", PUBLISHED), ("b.com", ORDERED)], keywords=KEYWORDS)
        dry = run(book, dry_run=True)
        assert Site.all_objects.count() == 0
        assert Placement.objects.count() == 0
        assert SiteList.objects.count() == 0
        real = run(book)
        assert dry.counts == real.counts
        assert dry.issues == real.issues
        assert TaskRun.objects.filter(status=TaskStatus.SUCCESS).count() == 2

    def test_command_writes_summary_and_report(
        self,
        make_workbook: MakeWorkbook,
        products: tuple[Product, Product],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        book = make_workbook(base=[("a.com", {})], keywords=KEYWORDS)
        call_command(
            "import_workbook",
            str(book),
            "--date=2026-09-27",
            f"--list={SEPTEMBER}",
            "--dry-run",
            f"--report-dir={tmp_path / 'reports'}",
        )
        output = capsys.readouterr().out
        assert "sites: создано 1" in output
        assert "Сухой прогон" in output
        [report_file] = (tmp_path / "reports").glob("*_dry-run.md")
        assert "Отчёт импорта таблицы" in report_file.read_text(encoding="utf-8")

    def test_command_reports_abort(self, make_workbook: MakeWorkbook) -> None:
        with pytest.raises(CommandError, match=r"convertio\.co"):
            call_command(
                "import_workbook",
                str(make_workbook()),
                "--date=2026-09-27",
                f"--list={SEPTEMBER}",
            )
