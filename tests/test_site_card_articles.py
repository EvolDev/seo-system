"""Статьи продуктов доступны прямо из шапки карточки площадки."""

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.placements import other_products
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site
from apps.workspace.products import choose_product
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db


@pytest.fixture
def records() -> tuple[Site, Product, Product]:
    convertio = Product.objects.create(name="Convertio", domain="convertio.co")
    clideo = Product.objects.create(name="Clideo", domain="clideo.com")
    return Site.objects.create(domain="articles.example"), convertio, clideo


def _placed(site: Site, product: Product, url: str | None, day: int | None = None) -> Placement:
    return Placement.objects.create(
        site=site,
        product=product,
        article_url=url,
        status=PlacementStatus.PLACED,
        published_at=dt.datetime(2026, 9, day, tzinfo=dt.UTC) if day else None,
    )


def test_articles_are_grouped_sorted_and_scoped(records: tuple[Site, Product, Product]) -> None:
    site, convertio, clideo = records
    _placed(site, clideo, "https://articles.example/latest", 20)
    _placed(site, convertio, "https://articles.example/convertio", 22)
    # Старая статья записана позже новой: порядок определяет дата публикации.
    _placed(site, clideo, "https://articles.example/old", 10)
    _placed(site, clideo, "https://articles.example/unknown-date")
    _placed(
        Site.objects.create(domain="different.example"),
        clideo,
        "https://different.example/post",
        25,
    )
    with CaptureQueriesContext(connection) as queries:
        articles = other_products.articles_by_product(site.pk)
    assert len(queries) == 1
    assert [a.url for a in articles[clideo.pk]] == [
        "https://articles.example/latest",
        "https://articles.example/old",
        "https://articles.example/unknown-date",
    ]
    assert [a.url for a in articles[convertio.pk]] == ["https://articles.example/convertio"]


def test_unknown_dates_use_latest_record(records: tuple[Site, Product, Product]) -> None:
    site, _, clideo = records
    _placed(site, clideo, "https://articles.example/first")
    _placed(site, clideo, "https://articles.example/second")
    assert other_products.articles_by_product(site.pk)[clideo.pk][0].url.endswith("/second")


@pytest.mark.parametrize(
    "url", [None, "", " ", "javascript:alert(1)", "/admin/local", "https://[invalid"]
)
def test_missing_or_non_web_urls_have_no_link(
    records: tuple[Site, Product, Product], url: str | None
) -> None:
    site, _, clideo = records
    _placed(site, clideo, url)
    assert other_products.articles_by_product(site.pk) == {}


@pytest.mark.parametrize("partial", [True, False])
def test_card_shows_latest_and_older_articles(
    admin_client: Client, admin_user: User, records: tuple[Site, Product, Product], partial: bool
) -> None:
    site, convertio, clideo = records
    choose_product(admin_user, convertio)
    _placed(site, clideo, "https://articles.example/old", 10)
    _placed(site, clideo, "https://articles.example/new", 20)
    response = admin_client.get(
        reverse("admin:sites_site_card", args=[site.pk]),
        headers={PARTIAL_HEADER: "1"} if partial else {},
    )
    assert response.status_code == 200
    page = response.content.decode()
    start = page.index('class="seo-card-other"')
    other = page[start : page.index("</header>", start)]
    assert 'class="seo-card-article-link" href="https://articles.example/new"' in other
    assert 'target="_blank" rel="noopener noreferrer"' in other
    assert '<details class="seo-card-articles">' in other
    assert "Ещё статьи (1)" in other
    assert other.index("https://articles.example/old") > other.index("<details")
    assert "10.09.2026" in other


def test_single_article_needs_no_spoiler(
    admin_client: Client, records: tuple[Site, Product, Product]
) -> None:
    site, _, clideo = records
    _placed(site, clideo, "https://articles.example/only")
    response = admin_client.get(reverse("admin:sites_site_card", args=[site.pk]))
    page = response.content.decode()
    assert 'href="https://articles.example/only"' in page
    assert '<details class="seo-card-articles">' not in page
