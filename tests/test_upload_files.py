"""Чтение загруженного файла и разбор значений прайсов (E1-08, ADR-044).

Файлы — синтетические, по образцу примеров из `import_examples/`: сами
примеры в git не идут (цены и контакты).
"""

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import Workbook

from apps.sites.models import PlacementType
from apps.sites.uploads import values
from apps.sites.uploads.columns import looks_like_header
from apps.sites.uploads.files import FileError, read_table


def _xlsx(path: Path, rows: list[list[object]]) -> Path:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


class TestReadTable:
    def test_header_under_sheet_title_like_2_xlsx(self, tmp_path: Path) -> None:
        path = _xlsx(
            tmp_path / "2.xlsx",
            [
                ["sites"],
                ["Domain", "Price", "Link type", "sponsored tag"],
                ["the-digital-reader.com", 180, "Dofollow", "No", "keterinapatra@gmail.com"],
                ["findarticles.com", 120, "Dofollow", "No"],
                [None, None, None, None],
            ],
        )
        table = read_table(path, is_header=looks_like_header)
        assert table.header_row == 2
        assert [c.header for c in table.columns] == [
            "Domain",
            "Price",
            "Link type",
            "sponsored tag",
            "Колонка E",
        ]
        assert [row.line for row in table.rows] == [3, 4]
        assert table.blank_rows == 1
        assert table.columns[4].samples == ("keterinapatra@gmail.com",)

    def test_csv_with_comma_inside_quoted_header_like_zain(self, tmp_path: Path) -> None:
        path = tmp_path / "zain.csv"
        path.write_text(
            'Сайт,Трафик,"Цена, $",Тип размещения,,,контакты\n'
            "https://www.portotheme.com/,21 400,$170.00,Both,,,Zain Mediax\n",
            encoding="utf-8",
        )
        table = read_table(path, is_header=looks_like_header)
        # Пустые колонки без заголовка и значений — не колонки.
        assert [c.header for c in table.columns] == [
            "Сайт",
            "Трафик",
            "Цена, $",
            "Тип размещения",
            "контакты",
        ]
        assert table.rows[0].get(2) == "$170.00"

    def test_windows_1251_and_semicolon(self, tmp_path: Path) -> None:
        path = tmp_path / "price.csv"
        path.write_bytes("Сайт;Цена\nexample.com;311,81\n".encode("cp1251"))
        table = read_table(path, is_header=looks_like_header)
        assert [c.header for c in table.columns] == ["Сайт", "Цена"]
        assert table.rows[0].cells == ("example.com", "311,81")

    def test_utf16_tab_like_ahrefs(self, tmp_path: Path) -> None:
        path = tmp_path / "refdomains.csv"
        path.write_bytes('"Domain"\t"DR"\n"google.com"\t"100"\n'.encode("utf-16"))
        table = read_table(path)
        assert [c.header for c in table.columns] == ["Domain", "DR"]
        assert table.rows[0].cells == ("google.com", "100")

    def test_utf8_bom_semicolon_with_quoted_commas_like_collaborator(self, tmp_path: Path) -> None:
        path = tmp_path / "collaborator.csv"
        path.write_bytes(
            'Domain;"Collaborator URL";Category\n'
            'https://www.tuttotek.it;https://collaborator.pro/x;"Electronics and Technology, '
            'PC and Video Games";;;\n'.encode("utf-8-sig")
        )
        table = read_table(path)
        assert [c.header for c in table.columns] == ["Domain", "Collaborator URL", "Category"]
        assert table.rows[0].get(2) == "Electronics and Technology, PC and Video Games"

    def test_same_header_twice_gets_number(self, tmp_path: Path) -> None:
        path = _xlsx(tmp_path / "x.xlsx", [["Site", "Price", "Price"], ["a.com", 1, 2]])
        assert [c.header for c in read_table(path).columns] == ["Site", "Price", "Price (2)"]

    def test_header_row_can_be_given(self, tmp_path: Path) -> None:
        path = _xlsx(tmp_path / "x.xlsx", [["Site", "Price"], ["Сайт", "Цена"], ["a.com", 1]])
        table = read_table(path, header_row=2)
        assert [c.header for c in table.columns] == ["Сайт", "Цена"]

    def test_excel_dates_and_numbers_are_shown_as_text(self, tmp_path: Path) -> None:
        path = _xlsx(
            tmp_path / "x.xlsx",
            [["Site", "Added", "DA"], ["a.com", dt.datetime(2026, 9, 27), 35.0]],
        )
        assert read_table(path).columns[1].samples == ("27.09.2026",)
        assert read_table(path).columns[2].samples == ("35",)

    @pytest.mark.parametrize("name", ["price.pdf", "price.xls"])
    def test_unsupported_format(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / name
        path.write_bytes(b"x")
        with pytest.raises(FileError, match="не поддерживается"):
            read_table(path)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.csv"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(FileError, match="пустой"):
            read_table(path)


class TestValues:
    @pytest.mark.parametrize(
        ("raw", "cents"),
        [
            ("$1 200", 120000),
            ("$1\xa0200", 120000),
            ("$170.00", 17000),
            ("311,81", 31181),
            ("18,2", 1820),
            ("1,200", 120000),
            ("1,200.50", 120050),
            ("1.200,50", 120050),
            ("€90", 9000),
            ("90 EUR", 9000),
            (186.05, 18605),
            (220, 22000),
            ("-", None),
            ("", None),
            (None, None),
        ],
    )
    def test_money(self, raw: object, cents: int | None) -> None:
        assert values.parse_money(raw) == cents

    @pytest.mark.parametrize("raw", ["$1 2OO", "abc", "-5", True])
    def test_bad_money(self, raw: object) -> None:
        with pytest.raises(ValueError, match="не"):
            values.parse_money(raw)

    def test_free_is_zero_only_for_writing_and_announce(self) -> None:
        assert values.parse_money("free", free_is_zero=True) == 0
        assert values.parse_money("бесплатно", free_is_zero=True) == 0
        with pytest.raises(ValueError):
            values.parse_money("free")

    @pytest.mark.parametrize(
        ("raw", "number"),
        [
            ("21 400", 21400),
            ("1\xa0072", 1072),
            ("31.9K", 31900),
            ("1.2M", 1200000),
            ("84,307,672", 84307672),
            (35.0, 35),
            (35.5, 36),
            ("n/a", None),
        ],
    )
    def test_number(self, raw: object, number: int | None) -> None:
        assert values.parse_number(raw) == number

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("Do-Follow", "dofollow"),
            ("Do Follow", "dofollow"),
            ("Dofollow", "dofollow"),
            ("No-Follow", "nofollow"),
            ("nofollow", "nofollow"),
        ],
    )
    def test_link_type(self, raw: str, kind: str) -> None:
        assert values.parse_link_type(raw) == kind

    @pytest.mark.parametrize(
        ("raw", "service"),
        [
            ("Link insertion", PlacementType.LINK_INSERTION),
            ("Guest Post", PlacementType.GUEST_POST),
            ("GP", PlacementType.GUEST_POST),
            ("Both", PlacementType.GUEST_POST),
            ("вставка ссылки", PlacementType.LINK_INSERTION),
        ],
    )
    def test_service(self, raw: str, service: PlacementType) -> None:
        assert values.parse_service(raw) == service

    def test_unknown_service(self) -> None:
        with pytest.raises(ValueError, match="услуга не распознана"):
            values.parse_service("Press release")

    @pytest.mark.parametrize(
        ("raw", "domain", "is_url"),
        [
            ("sheenmagazine.com ", "sheenmagazine.com", False),
            ("https://www.portotheme.com/", "portotheme.com", False),
            ("https://www.quickconvertnews.com/blog/", "quickconvertnews.com", True),
            ("HTTPS://Example.com/?ref=1", "example.com", True),
        ],
    )
    def test_domain(self, raw: str, domain: str, is_url: bool) -> None:
        assert values.parse_domain(raw) == (domain, is_url)

    @pytest.mark.parametrize("raw", ["", "keterinapatra@gmail.com", "news", "two words.com"])
    def test_bad_domain(self, raw: str) -> None:
        with pytest.raises(ValueError):
            values.parse_domain(raw)

    @pytest.mark.parametrize(
        ("text", "currency"),
        [
            ("Цена, $", "USD"),
            ("Price (EUR)", "EUR"),
            ("Цена на коллобораторе EUR", "EUR"),
            ("DR", None),
        ],
    )
    def test_currency_in(self, text: str, currency: str | None) -> None:
        assert values.currency_in(text) == currency
