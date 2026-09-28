"""Разбор значений ячеек при импорте таблицы (E1-04, маппинг §1–2)."""

import datetime as dt

import pytest

from apps.sites.importing.languages import language_code
from apps.sites.importing.values import (
    is_blank,
    is_rejection,
    is_url_on_domain,
    mentions_refusal,
    parse_announce_cents,
    parse_cents,
    parse_date,
    parse_int,
    parse_position,
    parse_yes_no,
    split_categories,
    split_languages,
    text,
)


class TestBlankAndText:
    @pytest.mark.parametrize("value", [None, "", "   ", "\n"])
    def test_blank(self, value: object) -> None:
        assert is_blank(value)
        assert text(value) is None

    def test_text_is_stripped(self) -> None:
        assert text("  Collaborator ") == "Collaborator"


class TestParseInt:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (96653, 96653),
            (96653.0, 96653),
            ("84,307,672", 84307672),
            ("24 195 783", 24195783),
            ("24 195 783", 24195783),
            ("1,209,017", 1209017),
            (" 55 ", 55),
            (None, None),
            ("", None),
        ],
    )
    def test_parsed(self, value: object, expected: int | None) -> None:
        assert parse_int(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "The article I have in mind: https://blog.designcrowd.com",
            "3,5",
            "12,34,567",
            12.5,
            True,
        ],
    )
    def test_not_a_number(self, value: object) -> None:
        with pytest.raises(ValueError):
            parse_int(value)


class TestMoney:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (18.2, 1820),
            (544.57, 54457),
            (0.02, 2),
            (2722.86, 272286),
            (90, 9000),
            ("45.38", 4538),
            (0.005, 1),
            (None, None),
            ("", None),
        ],
    )
    def test_cents(self, value: object, expected: int | None) -> None:
        assert parse_cents(value) == expected

    @pytest.mark.parametrize("value", ["бесплатно", "Бесплатно", " бесплатно "])
    def test_free_announce_is_zero(self, value: str) -> None:
        assert parse_announce_cents(value) == 0

    def test_announce_number_and_empty(self) -> None:
        assert parse_announce_cents(220.64) == 22064
        assert parse_announce_cents("") is None

    @pytest.mark.parametrize("value", ["дорого", "NaN", "-5", -5, True])
    def test_not_money(self, value: object) -> None:
        with pytest.raises(ValueError):
            parse_cents(value)


class TestDateAndYesNo:
    def test_date_from_text(self) -> None:
        assert parse_date("14.09.2026") == dt.date(2026, 9, 14)

    def test_date_from_excel_cell(self) -> None:
        assert parse_date(dt.datetime(2026, 9, 14, 0, 0)) == dt.date(2026, 9, 14)

    @pytest.mark.parametrize("value", ["2026-09-14", "14/09/2026", "Sep-26"])
    def test_not_a_date(self, value: str) -> None:
        with pytest.raises(ValueError):
            parse_date(value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("да", True), ("Да", True), (" ДА ", True), ("Нет", False), ("нет", False), ("", None)],
    )
    def test_yes_no_ignores_case(self, value: str, expected: bool | None) -> None:
        assert parse_yes_no(value) == expected

    def test_yes_no_rejects_other_words(self) -> None:
        with pytest.raises(ValueError):
            parse_yes_no("404 Ошибка")


class TestCategories:
    def test_comma_inside_name_is_kept(self) -> None:
        assert split_categories("Бизнес и финансы, СМИ (Новости), Общество, политика, законы") == [
            "Бизнес и финансы",
            "СМИ (Новости)",
            "Общество, политика, законы",
        ]

    def test_four_declared_topics(self) -> None:
        value = "Азартные игры, Кредитование, микрозаймы, Форекс, брокеры, Сайты знакомств"
        assert split_categories(value) == [
            "Азартные игры",
            "Кредитование, микрозаймы",
            "Форекс, брокеры",
            "Сайты знакомств",
        ]

    def test_comma_inside_brackets_is_kept(self) -> None:
        assert split_categories("Шопинг (сайты для покупок, Купоны), Технологии") == [
            "Шопинг (сайты для покупок, Купоны)",
            "Технологии",
        ]

    def test_empty(self) -> None:
        assert split_categories(None) == []
        assert split_categories("") == []

    def test_languages(self) -> None:
        assert split_languages("Украинский, Английский, Русский") == [
            "Украинский",
            "Английский",
            "Русский",
        ]
        assert split_languages(None) == []

    @pytest.mark.parametrize(
        ("name", "code"), [("Английский", "en"), ("Украинский", "uk"), (" Немецкий ", "de")]
    )
    def test_language_codes(self, name: str, code: str) -> None:
        assert language_code(name) == code

    def test_unknown_language(self) -> None:
        assert language_code("Кантонский") is None


class TestPosition:
    def test_regular_position(self) -> None:
        assert parse_position(7.0) == 7

    def test_out_of_top_mark_is_none(self) -> None:
        assert parse_position(101) is None

    @pytest.mark.parametrize("value", [0, 102, "", "n/a"])
    def test_invalid(self, value: object) -> None:
        with pytest.raises(ValueError):
            parse_position(value)


class TestUrlOnDomain:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.eggradients.com/blog/top-7",
            "http://eggradients.com/x",
            "https://blog.eggradients.com/x",
        ],
    )
    def test_on_domain(self, url: str) -> None:
        assert is_url_on_domain(url, "eggradients.com")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.dashclicks.com/blog/video-marketing",
            "(Convertio) https://eggradients.com/blog/what-is-a-webp-file",
            "eggradients.com/blog/biervideo-maken?preview=true",
            "https://noteggradients.com/x",
            "ftp://eggradients.com/x",
        ],
    )
    def test_not_on_domain(self, url: str) -> None:
        assert not is_url_on_domain(url, "eggradients.com")


class TestComments:
    @pytest.mark.parametrize(
        "comment",
        [
            "Nofollow, отбрасываем",
            "не тематика, не понятно что там анонсируют, отбрасываю",
            "Слишком дорого, Отбрасываем, мб в будущем",
        ],
    )
    def test_rejection(self, comment: str) -> None:
        assert is_rejection(comment)

    @pytest.mark.parametrize("comment", [None, "дорого", "блочит европу"])
    def test_not_rejection(self, comment: str | None) -> None:
        assert not is_rejection(comment)

    @pytest.mark.parametrize(
        "comment", ["Отказались писать", "Отказали, без объяснения", "отклонили заявку"]
    )
    def test_refusal(self, comment: str) -> None:
        assert mentions_refusal(comment)

    def test_not_refusal(self) -> None:
        assert not mentions_refusal("пивко всегда хорошо заходит, надо брать")
        assert not mentions_refusal(None)
