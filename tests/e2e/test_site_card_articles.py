"""Прямые ссылки открывают новую вкладку; спойлер показывает прежние статьи."""

import datetime as dt

import pytest
from django.urls import reverse
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.mark.parametrize("partial", [True, False])
def test_article_tabs_and_spoiler(admin_page: Page, live_server: LiveServer, partial: bool) -> None:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    clideo, _ = Product.objects.get_or_create(domain="clideo.com", defaults={"name": "Clideo"})
    site = Site.objects.create(domain="articles.example")
    for suffix, day in [("old", 10), ("latest", 20)]:
        Placement.objects.create(
            site=site,
            product=clideo,
            status=PlacementStatus.PLACED,
            article_url=f"https://articles.example/{suffix}",
            published_at=dt.datetime(2026, 9, day, tzinfo=dt.UTC),
        )
    page = admin_page
    page.set_viewport_size({"width": 1920, "height": 1000})
    page.context.route(
        "https://articles.example/**",
        lambda route: route.fulfill(content_type="text/html", body="Test article"),
    )
    if partial:
        page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
        page.locator("#result_list a[data-panel]", has_text=site.domain).first.click()
    else:
        page.goto(live_server.url + reverse("admin:sites_site_card", args=[site.pk]))
    original = page.url
    other = page.locator(".seo-card-other", has_text="Clideo")
    latest = other.get_by_role("link", name="Открыть статью ↗", exact=True)
    expect(latest).to_have_attribute("href", "https://articles.example/latest")
    old = other.locator('a[href="https://articles.example/old"]')
    expect(old).to_be_hidden()
    with page.expect_popup() as opened:
        latest.click()
    popup = opened.value
    popup.wait_for_load_state()
    assert popup.url == "https://articles.example/latest"
    popup.close()
    assert page.url == original
    expect(other).to_be_visible()
    other.locator(".seo-card-articles summary").click()
    expect(old).to_be_visible()
    with page.expect_popup() as opened:
        old.click()
    popup = opened.value
    popup.wait_for_load_state()
    assert popup.url == "https://articles.example/old"
    popup.close()
    assert page.url == original
    page.screenshot(
        path=f"/app/test-results/article-previews/card-{partial}-expanded.png", full_page=False
    )
    other.locator(".seo-card-articles summary").click()
    expect(old).to_be_hidden()
    expect(latest).to_be_visible()
    page.screenshot(path=f"/app/test-results/article-previews/card-{partial}.png", full_page=False)
