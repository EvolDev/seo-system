"""Чтение книги и разбор вкладок в данные для записи (E1-04, маппинг §1–2).

Книгу тест собирает сам (`make_workbook` в conftest): настоящий файл с
ценами и контактами в репозиторий не кладём.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.placements.models import PlacementStatus, PlacementType
from apps.sites.importing.report import Report, Section
from apps.sites.importing.rows import (
    BASE_REQUIRED,
    BASE_SHEET,
    COPY_REQUIRED,
    COPY_SHEET,
    KEYWORDS_REQUIRED,
    KEYWORDS_SHEET,
    KeywordData,
    Link,
    SiteData,
    parse_base,
    parse_copy,
    parse_keywords,
)
from apps.sites.importing.workbook import ImportAbort, open_workbook, position_columns, read_sheet

MakeWorkbook = Callable[..., Path]


def _base(path: Path) -> tuple[list[SiteData], Report]:
    report = Report()
    with open_workbook(path) as workbook:
        sites = parse_base(read_sheet(workbook, BASE_SHEET, BASE_REQUIRED), report)
    return sites, report


def _keywords(path: Path) -> tuple[list[KeywordData], Report]:
    report = Report()
    with open_workbook(path) as workbook:
        keywords = parse_keywords(read_sheet(workbook, KEYWORDS_SHEET, KEYWORDS_REQUIRED), report)
    return keywords, report


def _only(sites: list[SiteData]) -> SiteData:
    assert len(sites) == 1
    return sites[0]


class TestReadSheet:
    def test_missing_file(self, tmp_path: Path) -> None:
        with (
            pytest.raises(ImportAbort, match="Файл не найден"),
            open_workbook(tmp_path / "no.xlsx"),
        ):
            pass

    def test_missing_sheet(self, make_workbook: MakeWorkbook) -> None:
        with (
            open_workbook(make_workbook()) as workbook,
            pytest.raises(ImportAbort, match="Размещения"),
        ):
            read_sheet(workbook, COPY_SHEET, COPY_REQUIRED)

    def test_missing_column_is_named(self, make_workbook: MakeWorkbook) -> None:
        headers = [h for h in BASE_REQUIRED if h != "Тип ссылки статья"]
        path = make_workbook(base=[("a.com", {})], base_headers=headers)
        with open_workbook(path) as workbook, pytest.raises(ImportAbort, match="Тип ссылки статья"):
            read_sheet(workbook, BASE_SHEET, BASE_REQUIRED)

    def test_columns_found_by_header_not_position(self, make_workbook: MakeWorkbook) -> None:
        headers = list(reversed(BASE_REQUIRED))
        path = make_workbook(base=[("a.com", {"DR": 77})], base_headers=headers)
        site = _only(_base(path)[0])
        assert site.metrics.dr == 77
        assert site.card.link_type == "dofollow"

    def test_blank_rows_are_counted_not_returned(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(base=[("a.com", {}), {}, {}, ("b.com", {})])
        sites, report = _base(path)
        assert [site.domain for site in sites] == ["a.com", "b.com"]
        assert any("пустых строк пропущено — 2" in note for note in report.notes)

    def test_position_columns(self) -> None:
        headers = ["Keyword", "Pos 22.09.26", "Pos 02.09.26", "Pos total"]
        assert position_columns(headers) == {
            "Pos 22.09.26": dt.date(2026, 9, 22),
            "Pos 02.09.26": dt.date(2026, 9, 2),
        }


class TestSiteCard:
    def test_card_metrics_prices(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[
                (
                    "https://www.Example.com/",
                    {
                        "Языки сайта": "Украинский, Английский, Русский",
                        "Тематика": "Бизнес и финансы, СМИ (Новости), Общество, политика, законы",
                        "Особые тематики": "Азартные игры, Кредитование, микрозаймы",
                        "Пометка о рекламе статья": "Да",
                        "Organic / Traffic": "84,307,672",
                        "Цена размещения статья, EUR": 18.2,
                        "Цена анонса статья, EUR": "бесплатно",
                        "Цена написания статья, EUR": "",
                        "Количество ссылок статья": 2.0,
                    },
                )
            ]
        )
        site = _only(_base(path)[0])
        assert site.domain == "example.com"
        assert site.card.languages == ["Украинский", "Английский", "Русский"]
        assert site.card.language == "uk"
        assert site.card.topics == [
            "Бизнес и финансы",
            "СМИ (Новости)",
            "Общество, политика, законы",
        ]
        assert site.card.declared_topics == ["Азартные игры", "Кредитование, микрозаймы"]
        assert site.card.marks_as_ad is True
        assert site.card.links_allowed == 2
        assert site.metrics.organic_traffic == 84307672
        assert (site.prices.placement_cents, site.prices.announce_cents) == (1820, 0)
        assert site.prices.writing_cents is None

    def test_empty_declared_topics_is_none(self, make_workbook: MakeWorkbook) -> None:
        site = _only(_base(make_workbook(base=[("a.com", {})]))[0])
        assert site.card.declared_topics is None

    def test_unknown_language(self, make_workbook: MakeWorkbook) -> None:
        sites, report = _base(make_workbook(base=[("a.com", {"Языки сайта": "Кантонский"})]))
        assert _only(sites).card.language is None
        assert _only(sites).card.languages == ["Кантонский"]
        assert report.issues[Section.UNKNOWN_LANGUAGE] == ["a.com (строка 2): «Кантонский»"]

    def test_bad_number_goes_to_report(self, make_workbook: MakeWorkbook) -> None:
        sites, report = _base(make_workbook(base=[("a.com", {"DR": "много"})]))
        assert _only(sites).metrics.dr is None
        assert "«DR»" in report.issues[Section.BAD_VALUES][0]

    def test_no_ahrefs_data_zero_is_empty(self, make_workbook: MakeWorkbook) -> None:
        empty = {"Organic / Traffic": None, "Top Geo": None, "Top Geo Traff": None}
        path = make_workbook(
            base=[("a.com", {**empty, "Organic / Total Keywords": None, "US Traff": 0})]
        )
        sites, report = _base(path)
        assert _only(sites).metrics.us_traffic is None
        assert report.issues[Section.NO_AHREFS] == ["a.com (строка 2)"]

    def test_us_equals_geo_only_for_real_traffic(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[
                ("same.com", {"Top Geo": "in", "Top Geo Traff": 5000, "US Traff": 5000}),
                ("zeros.com", {"Top Geo": "fr", "Top Geo Traff": 0, "US Traff": 0}),
                ("us.com", {"Top Geo": "us", "Top Geo Traff": 5000, "US Traff": 5000}),
            ]
        )
        report = _base(path)[1]
        assert len(report.issues[Section.US_EQUALS_GEO]) == 1
        assert report.issues[Section.US_EQUALS_GEO][0].startswith("same.com")

    def test_total_price_zero(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(base=[("zero.com", {"Итог цена": 0}), ("ok.com", {})])
        assert _base(path)[1].issues[Section.TOTAL_PRICE_ZERO] == ["zero.com (строка 2)"]


class TestDuplicatesAndSkips:
    def test_identical_rows_merged(self, make_workbook: MakeWorkbook) -> None:
        sites, report = _base(make_workbook(base=[("a.com", {}), ("a.com", {})]))
        assert len(sites) == 1
        assert Section.DUPLICATES not in report.issues
        assert any("одинаковых строк слито — 1" in note for note in report.notes)

    def test_conflicting_rows_first_wins(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[
                ("a.com", {"Organic / Total Keywords": 100}),
                ("www.a.com", {"Organic / Total Keywords": 200}),
            ]
        )
        sites, report = _base(path)
        assert _only(sites).metrics.total_keywords == 100
        [line] = report.issues[Section.DUPLICATES]
        assert "строки 2 и 3" in line
        assert "«Organic / Total Keywords»: 100 / 200" in line

    def test_row_without_target_reported(self, make_workbook: MakeWorkbook) -> None:
        sites, report = _base(make_workbook(base=[{"DR": 50}, ("a.com", {})]))
        assert [site.domain for site in sites] == ["a.com"]
        assert report.issues[Section.SKIPPED_ROWS] == ["строка 2: нет Target"]


class TestPlacement:
    def test_published_with_two_links(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[
                (
                    "a.com",
                    {
                        "Статус": "Размещено",
                        "URL статьи": "https://a.com/post",
                        "Индексация": "да",
                        "Дата размещения": "14.09.2026",
                        "Тип ссылки": "Guest Post",
                        "Анкор1": "mp4 to mp3",
                        "Ссылка1": "https://convertio.co/mp4-mp3/",
                        "Анкор2": "convertio.co",
                        "Ссылка2": "https://convertio.co",
                    },
                )
            ]
        )
        placement = _only(_base(path)[0]).placement
        assert placement is not None
        assert placement.status == PlacementStatus.PUBLISHED
        assert placement.published_on == dt.date(2026, 9, 14)
        assert placement.is_indexed is True
        assert placement.placement_type == PlacementType.GUEST_POST
        assert placement.links == (
            Link(1, "mp4 to mp3", "https://convertio.co/mp4-mp3/"),
            Link(2, "convertio.co", "https://convertio.co"),
        )

    def test_anchor_without_status_is_planned(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(base=[("a.com", {"Анкор1": "gif to mp4", "Ссылка1": "https://c.co/"})])
        placement = _only(_base(path)[0]).placement
        assert placement is not None
        assert placement.status == PlacementStatus.PLANNED

    def test_no_placement_columns_no_placement(self, make_workbook: MakeWorkbook) -> None:
        assert _only(_base(make_workbook(base=[("a.com", {})]))[0]).placement is None

    def test_unknown_status_reported_not_created(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[("a.com", {"Статус": "404 Ошибка", "Анкор1": "convert", "Ссылка1": "https://c/"})]
        )
        sites, report = _base(path)
        assert _only(sites).placement is None
        assert report.issues[Section.UNKNOWN_STATUS] == [
            "a.com (строка 2): «404 Ошибка», анкоры: convert"
        ]

    def test_refusal_in_comment_reported(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            base=[
                (
                    "a.com",
                    {
                        "Статус": "Заявка отправлена",
                        "Комментарий к площадке": "Отказали, без объяснения",
                        "Анкор1": "Convertio",
                        "Ссылка1": "https://convertio.co/",
                    },
                )
            ]
        )
        sites, report = _base(path)
        assert _only(sites).placement is not None
        assert "Отказали, без объяснения" in report.issues[Section.REFUSALS][0]

    def test_anchor_without_url_reported(self, make_workbook: MakeWorkbook) -> None:
        sites, report = _base(make_workbook(base=[("a.com", {"Анкор1": "convert"})]))
        placement = _only(sites).placement
        assert placement is None
        assert "ссылка 1 без анкора или адреса" in report.issues[Section.SKIPPED_ROWS][0]


class TestKeywords:
    def test_positions_and_out_of_top(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            keywords=[
                ("convert", {"Links Placed": 5, "Links Waiting": 2}),
                ("photo converter", {"Pos 22.09.26": 101, "Pos 16.09.26": None}),
            ]
        )
        keywords, _ = _keywords(path)
        convert, photo = keywords
        assert convert.positions == {
            dt.date(2026, 9, 22): 7,
            dt.date(2026, 9, 16): 14,
            dt.date(2026, 9, 9): 5,
            dt.date(2026, 9, 2): 7,
        }
        assert (convert.links_placed, convert.links_waiting) == (5, 2)
        assert photo.positions[dt.date(2026, 9, 22)] is None
        assert dt.date(2026, 9, 16) not in photo.positions

    def test_broken_volume_is_empty_and_reported(self, make_workbook: MakeWorkbook) -> None:
        text = "The article I have in mind: https://blog.designcrowd.com"
        keywords, report = _keywords(
            make_workbook(keywords=[("convert heic to jpg", {"Volume": text})])
        )
        assert keywords[0].volume is None
        assert "«Volume»" in report.issues[Section.BAD_VALUES][0]

    def test_duplicate_keyword_reported(self, make_workbook: MakeWorkbook) -> None:
        keywords, report = _keywords(make_workbook(keywords=[("convert", {}), ("convert", {})]))
        assert len(keywords) == 1
        assert "повторяется" in report.issues[Section.DUPLICATES][0]


class TestCopySheet:
    def test_values_kept_for_comparison(self, make_workbook: MakeWorkbook) -> None:
        path = make_workbook(
            copy=[
                (
                    "www.a.com",
                    {"Статус": "404 Ошибка", "Индексация": "да", "Дата размещения": "14.09.2026"},
                )
            ]
        )
        report = Report()
        with open_workbook(path) as workbook:
            [row] = parse_copy(read_sheet(workbook, COPY_SHEET, COPY_REQUIRED), report)
        assert row.domain == "a.com"
        assert row.status == "404 Ошибка"
        assert row.is_indexed is True
        assert row.published_on == dt.date(2026, 9, 14)
