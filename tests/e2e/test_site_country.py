"""Страна площадки в карточке (ADR-082): выбор с флагами сохраняется сразу."""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Site

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.mark.parametrize("panel", [True, False])
def test_card_country_saves_on_pick(admin_page: Page, live_server: LiveServer, panel: bool) -> None:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    site = Site.objects.create(domain="noticias.example", country="pt")
    page = admin_page
    if panel:
        page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
        page.locator("#result_list a[data-panel]", has_text=site.domain).first.click()
        expect(page.locator(".seo-panel [data-site-card]")).to_be_visible()
    else:
        page.goto(f"{live_server.url}/admin/sites/site/{site.pk}/card/")
    tile = page.locator("[data-site-card] .seo-card-country")
    expect(tile.locator(".seo-country-input")).to_have_value("Португалия · PT")

    tile.locator(".seo-country-input").click()
    tile.locator(".seo-country-input").fill("brazil")
    page.screenshot(path=f"/app/test-results/site-country/open-{panel}.png")
    with page.expect_response(lambda response: "/card/country/" in response.url):
        tile.locator(".seo-country-list li", has_text="Бразилия").click()

    tile = page.locator("[data-site-card] .seo-card-country")
    expect(tile.locator(".seo-country-input")).to_have_value("Бразилия · BR")
    site.refresh_from_db()
    assert site.country == "br"
    page.screenshot(path=f"/app/test-results/site-country/saved-{panel}.png")
