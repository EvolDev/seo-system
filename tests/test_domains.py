"""Нормализация домена площадки (E1-01) и столбик доменов в поиске списка."""

import pytest

from apps.sites.domains import normalize_domain, pasted_domains


@pytest.mark.parametrize(
    "raw",
    [
        "example.com",
        "HTTPS://WWW.Example.com/",
        "http://example.com",
        "www.example.com",
        "example.com/",
        "  Example.COM  ",
        "https://www.example.com/blog/some-article?utm=1#top",
        "https://example.com:8443/",
        "//example.com",
        "example.com.",
    ],
)
def test_same_domain(raw: str) -> None:
    assert normalize_domain(raw) == "example.com"


def test_subdomain_kept() -> None:
    # Отбрасывается только www: блог на поддомене — другая площадка.
    assert normalize_domain("https://blog.example.com/") == "blog.example.com"


def test_www_only_at_start() -> None:
    assert normalize_domain("mywww.example.com") == "mywww.example.com"


@pytest.mark.parametrize("raw", ["", "   ", "https://", "/"])
def test_empty_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="домен"):
        normalize_domain(raw)


class TestPastedDomains:
    """Столбик доменов в поиске списка: каждый — как `normalize_domain`."""

    def test_column_from_spreadsheet(self) -> None:
        pasted = "fashiongonerogue.com\r\nwww.veed.io\r\nhttps://Lensa.com/blog?x=1\r\nveed.io\r\n"
        assert pasted_domains(pasted) == ["fashiongonerogue.com", "veed.io", "lensa.com"]

    def test_any_separator(self) -> None:
        pasted = "a.com, b.com;c.com\td.com e.com"
        assert pasted_domains(pasted) == ["a.com", "b.com", "c.com", "d.com", "e.com"]

    @pytest.mark.parametrize(
        "text",
        ["", "lensa", "lensa.com", "https://www.lensa.com/blog", "spacecoast daily", "a.com x"],
    )
    def test_one_domain_is_a_plain_search(self, text: str) -> None:
        assert pasted_domains(text) is None

    def test_header_stays_to_be_reported(self) -> None:
        # Заголовок столбца строк не найдёт — экран покажет его среди ненайденных.
        assert pasted_domains("Domain\nlensa.com\namasty.com") == [
            "domain",
            "lensa.com",
            "amasty.com",
        ]

    def test_empty_cells_skipped(self) -> None:
        assert pasted_domains("lensa.com\n—\n-\namasty.com") == ["lensa.com", "amasty.com"]
