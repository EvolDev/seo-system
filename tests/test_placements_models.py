"""Блок 2: размещения и ссылки в них (E1-02)."""

import datetime as dt
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError

from apps.keywords.models import AnchorType, Keyword
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site
from config.run_id import bind_run_id

pytestmark = pytest.mark.django_db


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


@pytest.fixture
def placement(site: Site, convertio: Product) -> Placement:
    return Placement.objects.create(site=site, product=convertio)


def _keyword(product: Product, text: str) -> Keyword:
    return Keyword.objects.create(product=product, keyword=text, target_url="https://x")


def _link(placement: Placement, **fields: object) -> PlacementLink:
    defaults = {"anchor": "mp4 to mp3", "target_url": "https://convertio.co/mp4-mp3/"}
    return PlacementLink.objects.create(placement=placement, **{**defaults, **fields})


class TestPlacement:
    def test_two_links_bound_to_keywords(self, placement: Placement, convertio: Product) -> None:
        mp3 = _keyword(convertio, "mp4 to mp3")
        video = _keyword(convertio, "online video converter")
        _link(placement, keyword=mp3, anchor_type=AnchorType.EXACT, link_index=1)
        _link(placement, keyword=video, anchor_type=AnchorType.DILUTED, link_index=2)
        links = placement.links.order_by("link_index")
        assert [link.keyword for link in links] == [mp3, video]
        assert list(mp3.links.all()) == [links[0]]

    def test_link_without_keyword(self, placement: Placement) -> None:
        # Безанкорная или брендовая ссылка — ключа нет, это нормально.
        link = _link(placement, anchor="convertio.co", anchor_type=AnchorType.BRANDED)
        link.full_clean()
        assert link.keyword is None

    def test_defaults(self, placement: Placement) -> None:
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.PLANNED
        assert placement.currency == "EUR"
        assert placement.ad_label_requested is False
        assert placement.is_indexed is None

    def test_article_on_subdomain_kept_as_is(self, placement: Placement) -> None:
        # Статья на поддомене площадки — реальный случай, хост с доменом не сверяем.
        url = "https://Blog.example.com/How-To/?utm=1"
        placement.article_url = url
        placement.full_clean()
        placement.save()
        placement.refresh_from_db()
        assert placement.article_url == url

    def test_cancelled_by_status(self, placement: Placement) -> None:
        _link(placement)
        placement.status = PlacementStatus.CANCELLED
        placement.save()
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.CANCELLED
        assert placement.links.count() == 1

    def test_not_deleted_with_links(self, placement: Placement) -> None:
        _link(placement)
        with pytest.raises(ProtectedError):
            placement.delete()


class TestRunId:
    def test_taken_from_current_chain(self, site: Site, convertio: Product) -> None:
        with bind_run_id(uuid4()) as run_id:
            placement = Placement.objects.create(site=site, product=convertio)
        placement.refresh_from_db()
        assert placement.run_id == run_id

    def test_empty_outside_chain(self, placement: Placement) -> None:
        placement.refresh_from_db()
        assert placement.run_id is None


class TestKeywordOfOtherProduct:
    def test_rejected(self, placement: Placement) -> None:
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        link = PlacementLink(
            placement=placement,
            keyword=_keyword(clideo, "video editor"),
            anchor="video editor",
            target_url="https://clideo.com/",
        )
        with pytest.raises(ValidationError) as error:
            link.full_clean()
        assert "keyword" in error.value.message_dict

    def test_unsaved_placement_is_checked(self, site: Site, convertio: Product) -> None:
        # Так проверяет форма админки: размещение ещё не сохранено.
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        link = PlacementLink(
            placement=Placement(site=site, product=convertio),
            keyword=_keyword(clideo, "video editor"),
            anchor="video editor",
            target_url="https://clideo.com/",
        )
        with pytest.raises(ValidationError):
            link.clean()

    def test_same_product_accepted(self, placement: Placement, convertio: Product) -> None:
        link = PlacementLink(
            placement=placement,
            keyword=_keyword(convertio, "mp4 to mp3"),
            anchor="mp4 to mp3",
            target_url="https://convertio.co/mp4-mp3/",
        )
        link.full_clean()


class TestLostAt:
    def test_written_once(self, placement: Placement) -> None:
        link = _link(placement)
        first = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
        assert link.mark_lost(first) is True
        assert link.lost_at == first
        # Повторная проверка первое время не перезаписывает.
        assert link.mark_lost(first + dt.timedelta(days=14)) is False
        link.refresh_from_db()
        assert link.lost_at == first

    def test_other_links_untouched(self, placement: Placement) -> None:
        lost, alive = _link(placement), _link(placement)
        lost.mark_lost(dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        alive.refresh_from_db()
        assert alive.lost_at is None
