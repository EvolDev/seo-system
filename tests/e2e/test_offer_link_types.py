"""Селекторы в карточке сохраняются сразу, в панели и на полной странице."""

import pytest
from django.urls import reverse
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement
from apps.sites import offers
from apps.sites.models import Product, Seller, Site, SitePrice

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.mark.parametrize("panel", [True, False])
def test_offer_select_autosaves(admin_page: Page, live_server: LiveServer, panel: bool) -> None:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    site = Site.objects.create(domain="link-types.example")
    offer = SitePrice.objects.create(
        site=site, seller=Seller.collaborator(), placement_cents=15000, link_type="dofollow"
    )
    other = SitePrice.objects.create(
        site=site, seller=Seller.objects.create(name="Other seller"), placement_cents=19000
    )
    offers.set_working_price(offer)
    page = admin_page
    if panel:
        page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
        page.locator("#result_list a[data-panel]", has_text=site.domain).first.click()
        expect(page.locator(".seo-panel [data-site-card]")).to_be_visible()
    else:
        page.goto(f"{live_server.url}/admin/sites/site/{site.pk}/card/")
    card = page.locator("[data-site-card]")
    selected = card.locator("select[data-link-type]").first
    expect(selected).to_have_value("dofollow")
    selected.select_option("nofollow")
    expect(card.locator("select[data-link-type]").first).to_have_value("nofollow")
    # Дождаться завершения ответа, иначе ORM прочитает до записи.
    expect(card.locator("select[data-link-type]").nth(1)).to_have_value("nofollow")
    offer.refresh_from_db()
    other.refresh_from_db()
    assert offer.link_type == "nofollow"
    assert other.link_type is None
    card.screenshot(path=f"/app/.link-type-previews/known-{panel}.png")
    card.locator("select[data-link-type]").first.select_option("")
    expect(card.locator("select[data-link-type]").nth(1)).to_have_value("")
    offer.refresh_from_db()
    assert offer.link_type is None and offer.link_type_manual
    page.screenshot(
        path=f"/app/.link-type-previews/link-types-{'panel' if panel else 'full'}.png",
        full_page=True,
    )


def test_placement_selector_and_column(admin_page: Page, live_server: LiveServer) -> None:
    product, _ = Product.objects.get_or_create(
        domain="convertio.co", defaults={"name": "Convertio"}
    )
    site = Site.objects.create(domain="placement-types.example")
    price = SitePrice.objects.create(
        site=site, seller=Seller.collaborator(), placement_cents=15000, link_type="dofollow"
    )
    offers.set_working_price(price)
    placement = Placement.objects.create(
        site=site, product=product, seller=price.seller, placement_type=price.placement_type
    )
    page = admin_page
    page.goto(live_server.url + reverse("admin:placements_placement_change", args=[placement.pk]))
    select = page.locator("select[name=link_type]")
    expect(select).to_have_value("dofollow")
    select.select_option("nofollow")
    page.locator("input[name=_save]").click()
    expect(page.locator("#result_list")).to_be_visible()
    placement.refresh_from_db()
    assert placement.link_type == "nofollow" and placement.link_type_manual
    headers = page.locator("#result_list thead th").all_text_contents()
    paid = next(i for i, value in enumerate(headers) if "Заплачено" in value)
    assert "Тип ссылки" in headers[paid + 1]
    expect(page.locator("#result_list tbody")).to_contain_text("nofollow")
    page.screenshot(path="/app/.link-type-previews/link-types-placements.png", full_page=True)
