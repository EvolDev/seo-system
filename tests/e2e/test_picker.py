"""Список с поиском (seo/picker.js): продавца находят по имени, а не глазами."""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Seller

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture
def many_sellers() -> list[Seller]:
    names = ["Artur Links", "Athena smith", "Authlinker", "BackLink prov", "WM Links", "uniguide"]
    return [Seller.objects.create(name=name, currency="EUR") for name in names]


def test_search_finds_the_seller(
    admin_page: Page, live_server: LiveServer, many_sellers: list[Seller]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/")
    picker = page.locator(".seo-picker").first
    search = picker.locator(".seo-picker-input")
    expect(search).to_be_visible()

    search.click()
    search.fill("backl")
    items = picker.locator(".seo-picker-list li")
    expect(items).to_have_count(1)
    expect(items.first).to_have_text("BackLink prov")

    items.first.click()
    expect(search).to_have_value("BackLink prov")
    chosen = page.locator("#id_seller")
    expect(chosen).to_have_value(str(Seller.objects.get(name="BackLink prov").pk))


def test_nothing_found_is_said(
    admin_page: Page, live_server: LiveServer, many_sellers: list[Seller]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/")
    search = page.locator(".seo-picker").first.locator(".seo-picker-input")
    search.click()
    search.fill("нетакого")
    expect(page.locator(".seo-picker-list li.seo-picker-empty").first).to_have_text(
        "Ничего не нашлось"
    )
