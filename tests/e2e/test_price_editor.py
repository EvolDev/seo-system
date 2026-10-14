"""Редактор предложения в панели и на полной странице."""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites import offers
from apps.sites.models import Product, Seller, Site, SitePrice

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.mark.parametrize("panel", [True, False])
def test_price_editor(admin_page: Page, live_server: LiveServer, panel: bool) -> None:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    site = Site.objects.create(domain="iprovpn-editor.example")
    original = SitePrice.objects.create(
        site=site,
        seller=Seller.objects.create(name="Katerina Patra"),
        placement_cents=80000,
        currency="USD",
        link_type="dofollow",
    )
    other = SitePrice.objects.create(site=site, seller=Seller.collaborator(), placement_cents=67329)
    offers.set_working_price(original)
    page = admin_page
    if panel:
        page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
        page.locator("#result_list a[data-panel]", has_text=site.domain).first.click()
    else:
        page.goto(f"{live_server.url}/admin/sites/site/{site.pk}/card/")
    card = page.locator("[data-site-card]")
    editor = card.locator("[data-price-editor]").first
    editor.locator("[data-price-open]").click()
    form = editor.locator("[data-price-form]")
    expect(form).to_be_visible()
    expect(form.locator("[name=amount]")).to_be_focused()
    form.locator("[name=amount]").fill("950")
    form.locator("[data-price-cancel]").click()
    expect(form).to_be_hidden()
    editor.locator("[data-price-open]").click()
    expect(form.locator("[name=amount]")).to_have_value("800.00")
    form.locator("[name=amount]").fill("-1")
    form.get_by_role("button", name="Сохранить", exact=True).click()
    expect(card.locator("[role=alert]")).to_be_visible()
    form = card.locator("[data-price-form]").first
    expect(form).to_be_visible()
    expect(form.locator("[name=amount]")).to_have_value("-1")
    assert SitePrice.objects.filter(site=site).count() == 2
    form.locator("[name=amount]").fill("900,00")
    page.screenshot(
        path=f"/app/test-results/price-previews/editing-{panel}.png",
        full_page=True,
        animations="disabled",
    )
    form.get_by_role("button", name="Сохранить", exact=True).click()
    expect(card.locator(".seo-card-big")).to_contain_text("$900")
    expect(card.locator("[data-price-form]").first).to_be_hidden()
    site.refresh_from_db()
    assert site.price is not None and site.price.placement_cents == 90000
    original.refresh_from_db()
    assert original.placement_cents == 80000
    history = card.locator("details").filter(has=page.locator("summary", has_text="История цен"))
    history.locator("summary").click()
    expect(history).to_contain_text("$800")
    # Другой продавец: меняется предложение, выбранная рабочая цена остаётся.
    row = (
        card.locator("tr")
        .filter(has=page.locator("[data-price-editor]"))
        .filter(has_text="Collaborator")
    )
    # В строке три карандаша — цена, написание, анонс (ADR-071); нужен у цены.
    price_editor = row.locator("[data-price-editor]:not(.seo-extra-editor)")
    price_editor.locator("[data-price-open]").click()
    price_editor.locator("[name=amount]").fill("700.25")
    price_editor.get_by_role("button", name="Сохранить", exact=True).click()
    expect(card.locator("tr", has_text="Collaborator").first).to_contain_text("€700.25")
    site.refresh_from_db()
    assert site.price is not None and site.price.placement_cents == 90000
    other.refresh_from_db()
    assert other.placement_cents == 67329
    page.screenshot(
        path=f"/app/test-results/price-previews/saved-{panel}.png",
        full_page=True,
        animations="disabled",
    )
