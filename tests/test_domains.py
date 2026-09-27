"""Нормализация домена площадки (E1-01)."""

import pytest

from apps.sites.domains import normalize_domain


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
