"""Граф слияния продавцов (E1-17): выбор главного щелчком, карточка при наведении."""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Seller, Site, SitePrice

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture
def duplicates() -> list[Seller]:
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    found = []
    for index, name in enumerate(("Saket Aggarwal", "Saket Aggrwal", "WM Links")):
        seller = Seller.objects.create(name=name, currency="EUR", contacts=f"{index}@mail.com")
        for number in range(index + 1):
            site = Site.objects.create(domain=f"s{index}-{number}.com")
            SitePrice.objects.create(site=site, seller=seller, placement_cents=10000 + number)
        found.append(seller)
    return found


def _url(live_server: LiveServer, sellers: list[Seller]) -> str:
    query = "&".join(f"id={seller.pk}" for seller in sellers)
    return f"{live_server.url}/admin/sites/seller/merge/?{query}"


def test_click_makes_the_node_main(
    admin_page: Page, live_server: LiveServer, duplicates: list[Seller]
) -> None:
    page = admin_page
    page.goto(_url(live_server, duplicates))
    # Предложен самый «тяжёлый» — у него больше всех цен.
    heaviest = duplicates[-1]
    expect(page.locator(f'[data-node="{heaviest.pk}"].is-target')).to_be_visible()

    other = duplicates[0]
    page.locator(f'[data-node="{other.pk}"] .seo-merge-card').click()
    expect(page.locator(f'[data-node="{other.pk}"].is-target')).to_be_visible()
    expect(page.locator(f'[data-node="{heaviest.pk}"].is-target')).to_have_count(0)
    # Стрелка главного спрятана: ему некуда вести.
    expect(page.locator(f'[data-arrow="{other.pk}"].is-off')).to_have_count(1)
    expect(page.locator("[data-result-name]")).to_have_text(other.name)


def test_hover_shows_the_card(
    admin_page: Page, live_server: LiveServer, duplicates: list[Seller]
) -> None:
    page = admin_page
    page.goto(_url(live_server, duplicates))
    tip = page.locator(f"#seo-merge-tip-{duplicates[0].pk}")
    expect(tip).to_be_hidden()
    page.locator(f'[data-node="{duplicates[0].pk}"] .seo-merge-card').hover()
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("Контакты")


def test_merge_moves_prices(
    admin_page: Page, live_server: LiveServer, duplicates: list[Seller]
) -> None:
    page = admin_page
    page.goto(_url(live_server, duplicates))
    target = duplicates[0]
    page.locator(f'[data-node="{target.pk}"] .seo-merge-card').click()
    page.get_by_role("button", name="Объединить").click()
    page.wait_for_url("**/admin/sites/seller/")
    expect(page.locator(".messagelist")).to_contain_text("Объединено в «Saket Aggarwal»")
    assert SitePrice.objects.filter(seller=target).count() == 6
    assert Seller.objects.filter(pk__in=[s.pk for s in duplicates[1:]]).count() == 0
