"""Разметка колонок, разбор строк прайса и каталога Collaborator (E1-08, ADR-044)."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from openpyxl import Workbook

from apps.sites.models import PlacementType
from apps.sites.uploads import catalog
from apps.sites.uploads.columns import (
    Confidence,
    Field,
    build_mapping,
    detect_currency,
    guess_columns,
    looks_like_header,
    validate_mapping,
)
from apps.sites.uploads.files import Table, read_table
from apps.sites.uploads.records import parse_price_list

# Заголовки трёх примеров из import_examples/.
HEADERS_1 = [
    "Website", "Category", "Country", "DA", "PA", "DR", "RD", "TF", "CF", "SS",
    "Semrush Traffic", "Ahref Traffic", "GP Price", "LI Price", "Grey Price",
    "Rel Attribute", "Sponsor Tag", "Type of Website", "Graph",
]  # fmt: skip
HEADERS_ZAIN = [
    "Сайт", "Трафик", "Цена, $", "Тип размещения", "Комментарий",
    "Цена на коллобораторе EUR", "Анонс", "Написание", "контакты",
]  # fmt: skip


def _table(tmp_path: Path, rows: Sequence[Sequence[object]], name: str = "price.xlsx") -> Table:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    path = tmp_path / name
    book.save(path)
    return read_table(path, is_header=looks_like_header)


def _fields(table: Table) -> dict[str, Field]:
    return {key: guess.field for key, guess in guess_columns(table.columns).items()}


class TestGuess:
    def test_linkhub_like_1_xlsx(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [HEADERS_1, ["a.com", *["1"] * 18]])
        fields = _fields(table)
        assert fields["website"] == Field.DOMAIN
        assert fields["dr"] == Field.DR
        assert fields["gp price"] == Field.GUEST_POST
        assert fields["li price"] == Field.LINK_INSERTION
        assert fields["grey price"] == Field.GRAY
        assert fields["rel attribute"] == Field.LINK_TYPE
        # Трафик Semrush и Ahrefs: поле одно — достаётся Ahrefs, Semrush — в прочие.
        assert fields["ahref traffic"] == Field.TRAFFIC
        assert fields["semrush traffic"] == Field.EXTRA
        # «Type of Website» — не услуга.
        assert fields["type of website"] == Field.EXTRA
        assert fields["da"] == Field.EXTRA

    def test_zain_service_column_makes_price_a_service_price(self, tmp_path: Path) -> None:
        table = _table(
            tmp_path, [HEADERS_ZAIN, ["a.com", "1", "$1", "Both", "x", "1", "1", "1", "x"]]
        )
        guesses = guess_columns(table.columns)
        assert guesses["цена, $"].field == Field.PRICE
        assert guesses["тип размещения"].field == Field.SERVICE
        assert guesses["комментарий"].field == Field.NOTE
        # Пометка коллеги о цене Collaborator — не цена продавца.
        assert guesses["цена на коллобораторе eur"].field == Field.EXTRA
        # Написание и анонс бывают в другой валюте — только «похоже».
        assert guesses["анонс"].confidence == Confidence.LIKELY
        assert guesses["трафик"].hint

    def test_price_without_service_is_guest_post(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Domain", "Price"], ["a.com", 1]])
        guess = guess_columns(table.columns)["price"]
        assert (guess.field, guess.confidence) == (Field.GUEST_POST, Confidence.LIKELY)


class TestMemory:
    """Разметка у продавца: второй файл — без вопросов, новая колонка — один вопрос."""

    def test_second_file_with_same_headers_asks_nothing(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Domain", "Price", "Notes"], ["a.com", 1, "x"]])
        guesses, questions = build_mapping(table.columns, None)
        assert questions == ["domain", "price", "notes"]
        remembered = {key: guess.field.value for key, guess in guesses.items()}
        _, questions = build_mapping(table.columns, remembered)
        assert questions == []

    def test_new_column_is_one_question(self, tmp_path: Path) -> None:
        remembered = {"domain": "domain", "price": "guest_post_price"}
        table = _table(tmp_path, [["Domain", "Price", "DA"], ["a.com", 1, 30]])
        guesses, questions = build_mapping(table.columns, remembered)
        assert questions == ["da"]
        assert guesses["price"].confidence == Confidence.REMEMBERED

    def test_empty_new_column_is_not_a_question(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Domain", "Price", "Grey"], ["a.com", 1, None]])
        _, questions = build_mapping(table.columns, {"domain": "domain", "price": "price"})
        assert questions == []


class TestGuessByValues:
    """Заголовок незнакомый — смотрим на значения (прайс Zain MediaX, 06.10.2026)."""

    @pytest.fixture
    def zain(self, tmp_path: Path) -> Table:
        return _table(
            tmp_path,
            [
                ["BLOG", "Traffic", "Price USD", "Post Type"],
                ["https://www.portotheme.com/", 21400, "$170.00", "Both"],
                ["https://amasty.com/", 88800, "$320.00", "Link insertion"],
                ["https://www.socialchamp.com/", 27000, "$320.00", "Link insertion"],
            ],
        )

    def test_urls_are_the_site_column(self, zain: Table) -> None:
        guesses = guess_columns(zain.columns)
        assert guesses["blog"].field == Field.DOMAIN
        assert guesses["blog"].confidence == Confidence.LIKELY

    def test_services_are_the_service_column(self, zain: Table) -> None:
        assert guess_columns(zain.columns)["post type"].field == Field.SERVICE

    def test_price_follows_the_service_column(self, zain: Table) -> None:
        # Раз услуга есть в файле, цена — за неё, а не «публикация».
        assert guess_columns(zain.columns)["price usd"].field == Field.PRICE

    def test_the_whole_file_passes_validation(self, zain: Table) -> None:
        mapping = {key: guess.field for key, guess in guess_columns(zain.columns).items()}
        assert validate_mapping(mapping, zain.columns) == []


class TestValidate:
    def test_needs_domain_and_price(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Site", "DR"], ["a.com", 1]])
        errors = validate_mapping({"site": Field.EXTRA, "dr": Field.DR}, table.columns)
        assert any("площадкой" in e for e in errors)
        assert any("ценой" in e for e in errors)

    def test_price_and_service_go_together(self, tmp_path: Path) -> None:
        """Ошибка говорит, что именно отметить, — а не только что «вместе»."""
        table = _table(tmp_path, [["Site", "Price"], ["a.com", 1]])
        errors = validate_mapping({"site": Field.DOMAIN, "price": Field.PRICE}, table.columns)
        assert any("Отметьте колонку, где написано" in e for e in errors)

        table = _table(tmp_path, [["Site", "Price", "Type"], ["a.com", 1, "Both"]])
        mapping = {"site": Field.DOMAIN, "price": Field.GUEST_POST, "type": Field.SERVICE}
        errors = validate_mapping(mapping, table.columns)
        assert any("размечена как услуга" in e for e in errors)

    def test_one_column_per_field(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Site", "A", "B"], ["a.com", 1, 2]])
        mapping = {"site": Field.DOMAIN, "a": Field.GUEST_POST, "b": Field.GUEST_POST}
        assert any("нескольких колонок" in e for e in validate_mapping(mapping, table.columns))

    def test_currency_from_header_values_or_seller(self, tmp_path: Path) -> None:
        table = _table(tmp_path, [["Site", "Цена, $"], ["a.com", 1]])
        mapping = {"site": Field.DOMAIN, "цена, $": Field.GUEST_POST}
        assert detect_currency(table.columns, mapping, "EUR")[0] == "USD"
        table = _table(tmp_path, [["Site", "Price"], ["a.com", "€90"]])
        mapping = {"site": Field.DOMAIN, "price": Field.GUEST_POST}
        assert detect_currency(table.columns, mapping, "USD")[0] == "EUR"
        table = _table(tmp_path, [["Site", "Price"], ["a.com", 90]])
        assert detect_currency(table.columns, mapping, "USD") == (
            "USD",
            "валюта продавца по умолчанию",
        )


class TestParsePriceList:
    def test_zain_like_rows(self, tmp_path: Path) -> None:
        table = _table(
            tmp_path,
            [
                HEADERS_ZAIN,
                ["https://www.selfcad.com/", "16 000", "$350.00", "Both", "дорого", "330"],
                ["https://amasty.com/", "88 800", "$320", "Link insertion", *[None] * 4, "Xavier"],
                ["https://titan.email/", "1 000", "$300.00"],
                ["https://www.quickconvertnews.com/blog/", "12 000", "$130", "Guest post"],
            ],
        )  # fmt: skip
        mapping = _fields(table) | {"анонс": Field.EXTRA, "написание": Field.EXTRA}
        parsed = parse_price_list(table, mapping, "USD")
        selfcad, amasty, quick = parsed.records
        # «Both» — публикация, пометка — в прочих данных (Q21).
        assert selfcad.offers == {PlacementType.GUEST_POST: 35000}
        assert selfcad.extra["Тип размещения"] == "Both"
        assert selfcad.extra["Цена на коллобораторе EUR"] == "330"
        assert selfcad.note == "дорого"
        assert selfcad.metrics == {"organic_traffic": 16000}
        assert amasty.offers == {PlacementType.LINK_INSERTION: 32000}
        assert amasty.extra == {"контакты": "Xavier"}
        assert quick.source_value == "https://www.quickconvertnews.com/blog/"
        assert [(e.line, e.message) for e in parsed.errors] == [
            (4, "titan.email: не указана услуга в колонке «Тип размещения»")
        ]

    def test_duplicate_takes_lower_price_and_reports_both(self, tmp_path: Path) -> None:
        table = _table(
            tmp_path,
            [["Domain", "Price"], ["aijourn.com", 180], ["x.com", 50], ["aijourn.com", 150]],
        )
        parsed = parse_price_list(table, _fields(table), "USD")
        assert {r.domain: r.offers[PlacementType.GUEST_POST] for r in parsed.records} == {
            "aijourn.com": 15000,
            "x.com": 5000,
        }
        (duplicate,) = parsed.duplicates
        assert duplicate.lines == (2, 4)
        assert duplicate.prices == ("публикация 180", "публикация 150")

    def test_bad_rows_are_errors_with_line(self, tmp_path: Path) -> None:
        table = _table(
            tmp_path,
            [["Website", "GP Price"], [None, "$100"], ["ok.com", "$1 2OO"], ["fine.com", None]],
        )
        parsed = parse_price_list(table, _fields(table), "USD")
        assert parsed.records == []
        messages = {e.line: e.message for e in parsed.errors}
        assert messages[2] == "нет площадки"
        assert "«GP Price»" in messages[3]
        assert messages[4] == "fine.com: нет цены"

    def test_no_nonempty_column_is_lost(self, tmp_path: Path) -> None:
        """Каждая непустая колонка — в своём поле или в прочих данных."""
        row = ["a.com", "News", "France", 26, 44, 21, 763, 24, 31, 0.01, "31.9K", 6827]
        row += ["$186.05", 50, None, "Do Follow", "No Tag", "Ultra High Quality Sites", "x"]
        table = _table(tmp_path, [HEADERS_1, row])
        mapping = _fields(table)
        parsed = parse_price_list(table, mapping, "USD")
        (record,) = parsed.records
        stored = set(record.extra)
        for column in table.columns:
            assert mapping[column.key] != Field.SKIP
            if mapping[column.key] == Field.EXTRA:
                stored.discard(column.header)
        assert stored == set()
        assert set(record.extra) == {
            c.header for c in table.columns if mapping[c.key] == Field.EXTRA
        }


class TestCatalog:
    def _catalog(self, tmp_path: Path, row: dict[str, str]) -> Table:
        headers = [*catalog.REQUIRED, "TF", "Monthly Traffic"]
        values = [row.get(h, "") for h in headers]
        path = tmp_path / "collaborator.csv"
        path.write_text(
            ";".join(f'"{h}"' for h in headers) + "\n" + ";".join(f'"{v}"' for v in values) + "\n",
            encoding="utf-8-sig",
        )
        return read_table(path, is_header=catalog.looks_like_catalog)

    def test_row_is_translated_to_russian(self, tmp_path: Path) -> None:
        table = self._catalog(
            tmp_path,
            {
                "Domain": "https://www.tuttotek.it",
                "Collaborator URL": "https://collaborator.pro/creator/article/view?id=60954",
                "Category": "Business and Finance, Society, Politics, and Laws, Mobile Technology",
                "Website languages": "Ukrainian, Russian",
                "Website type": "Personal blog",
                "Special categories": "Legal Betting and Casino, Lending and Microloans",
                "Link type article": "dofollow",
                "Advertising mark article": "Yes",
                "Number of links article": "2",
                "Publishing price article, EUR": "112.71",
                "Writing price article, EUR": "13.92",
                "Announcement price article, EUR": "free",
                "Sensitive topics price, EUR": "41.75",
                "DR": "55",
                "Organic traffic": "287664",
                "Keywords": "5445",
                "TF": "32",
            },
        )
        assert catalog.missing_columns(table) == []
        unknown = catalog.Unknown()
        (record,) = catalog.parse_catalog(table, unknown).records
        assert record.domain == "tuttotek.it"
        assert record.offers == {PlacementType.GUEST_POST: 11271}
        assert (record.writing_cents, record.announce_cents, record.gray_cents) == (1392, 0, 4175)
        assert record.metrics == {"dr": 55, "organic_traffic": 287664, "total_keywords": 5445}
        assert record.card == {
            "source": "Collaborator",
            "collaborator_url": "https://collaborator.pro/creator/article/view?id=60954",
            "topics": ["Бизнес и финансы", "Общество, политика, законы", "Мобильные технологи"],
            "declared_topics": ["Азартные игры", "Кредитование, микрозаймы"],
            "languages": ["Украинский", "Русский"],
            "language": "uk",
            "site_type": "Персональный блог",
            "links_allowed": 2,
            "link_type": "dofollow",
            "marks_as_ad": True,
        }
        # Пустая колонка в прочие данные не идёт, непустая — идёт.
        assert record.extra == {"TF": "32"}
        assert unknown.lines() == []

    def test_unknown_values_are_kept_and_reported(self, tmp_path: Path) -> None:
        table = self._catalog(
            tmp_path,
            {
                "Domain": "a.com",
                "Category": "Internet, Space Travel",
                "Website languages": "Klingon",
                "Website type": "Publisher",
                "Publishing price article, EUR": "10",
                "Link type article": "dofollow",
            },
        )
        unknown = catalog.Unknown()
        (record,) = catalog.parse_catalog(table, unknown).records
        assert record.card is not None
        assert record.card["topics"] == ["Интернет", "Space Travel"]
        assert record.card["site_type"] == "Publisher"
        assert record.card["language"] is None
        assert unknown.lines() == [
            "тематика «Space Travel» — 1",
            "тип сайта «Publisher» — 1",
            "язык «Klingon» — 1",
        ]

    def test_missing_columns(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        path.write_text("Domain;Price\na.com;1\n", encoding="utf-8")
        table = read_table(path)
        assert "Collaborator URL" in catalog.missing_columns(table)


@pytest.mark.parametrize(
    ("dictionary", "size"),
    [(catalog.TOPICS, 44), (catalog.SPECIAL_TOPICS, 4), (catalog.SITE_TYPES, 6)],
)
def test_dictionaries_cover_the_export(dictionary: dict[str, str], size: int) -> None:
    """Словари сверены с выгрузкой 01.10.2026: 44 тематики, 4 особые, 6 типов из 7."""
    assert len(dictionary) == size
    assert len(set(dictionary.values())) == size
