"""Какое размещение дополнять — общее правило импорта и загрузки размещений (E1-09, ADR-051)."""

import pytest

from apps.placements.matching import (
    AMBIGUOUS,
    NEW,
    match_placement,
    match_without_url,
    moves_forward,
    same_article,
)
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = pytest.mark.django_db


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="a.com")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _placement(site: Site, product: Product, url: str | None = None) -> Placement:
    return Placement.objects.create(
        site=site, product=product, status=PlacementStatus.PLACED, article_url=url
    )


class TestMatchPlacement:
    def test_same_article_in_other_notation(self, site: Site, clideo: Product) -> None:
        placement = _placement(site, clideo, "https://a.com/post/")
        other = _placement(site, clideo, "https://a.com/other")
        found = match_placement([other, placement], "http://www.a.com/post#:~:text=Clideo")
        assert found.placement == placement
        assert not found.ambiguous

    def test_single_without_url_gets_the_article(self, site: Site, clideo: Product) -> None:
        placement = _placement(site, clideo)
        assert match_placement([placement], "https://a.com/post").placement == placement

    def test_other_article_is_a_new_placement(self, site: Site, clideo: Product) -> None:
        # podchaser.com: у Clideo две разные статьи на одной площадке.
        _placement(site, clideo, "https://a.com/first")
        assert match_placement(list(Placement.objects.all()), "https://a.com/second") == NEW

    def test_two_without_url_are_ambiguous(self, site: Site, clideo: Product) -> None:
        candidates = [_placement(site, clideo), _placement(site, clideo)]
        assert match_placement(candidates, "https://a.com/post") == AMBIGUOUS

    def test_row_without_url_follows_import_rule(self, site: Site, clideo: Product) -> None:
        # Импорт таблицы: строка без адреса на площадке со статьёй — новое размещение.
        _placement(site, clideo, "https://a.com/post")
        assert match_placement(list(Placement.objects.all()), None) == NEW


class TestMatchWithoutUrl:
    def test_single_placement_even_with_url(self, site: Site, clideo: Product) -> None:
        placement = _placement(site, clideo, "https://a.com/post")
        assert match_without_url([placement]).placement == placement

    def test_none_is_new(self) -> None:
        assert match_without_url([]) == NEW

    def test_several_prefer_the_one_without_url(self, site: Site, clideo: Product) -> None:
        bare = _placement(site, clideo)
        found = match_without_url([_placement(site, clideo, "https://a.com/post"), bare])
        assert found.placement == bare

    def test_several_with_urls_are_ambiguous(self, site: Site, clideo: Product) -> None:
        candidates = [
            _placement(site, clideo, "https://a.com/first"),
            _placement(site, clideo, "https://a.com/second"),
        ]
        assert match_without_url(candidates) == AMBIGUOUS


def test_same_article() -> None:
    assert same_article("https://www.a.com/post/?utm_source=x", "http://a.com/post")
    assert not same_article("https://a.com/post", "https://a.com/Post")


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (PlacementStatus.ORDERED, PlacementStatus.PLACED, True),
        (PlacementStatus.PLACED, PlacementStatus.ORDERED, False),
        (PlacementStatus.PLACED, PlacementStatus.PLACED, False),
        (PlacementStatus.REJECTED, PlacementStatus.PLACED, False),
    ],
)
def test_moves_forward(current: str, target: str, expected: bool) -> None:
    assert moves_forward(current, target) is expected
