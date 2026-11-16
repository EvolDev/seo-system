"""Поиск по длинному фильтру с галочками (E1-24) — seo/multifilter.js.

Поле сужает пункты на месте. Проверка браузерная не для галочки: первая
версия прятала пункты атрибутом `hidden`, а `display: flex` в стилях его
перебивал — на экране не менялось ничего, и модульным тестом это не видно.
"""

import datetime as dt

import pytest
from django.utils import timezone
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import MetricSource, Product, Seller, Site, SitePrice
from apps.sites.uploads.plan import start_of_day

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

SITES = "/admin/sites/productsitelatest/"
SEARCH = ".seo-multi-search"
ITEMS = ".seo-multi-item:not([hidden])"
NAMED = ["Athena Smith", "Chaudhary Adeel", "Authlinker", "BackLink prov", "Zain MediaX"]


@pytest.fixture
def sellers() -> None:
    """Продавцы с ценами: фильтр показывает только тех, у кого есть площадки."""
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    names = NAMED + [f"Seller {n:02}" for n in range(9)]
    for number, name in enumerate(names):
        seller = Seller.objects.create(name=name)
        site = Site.objects.create(domain=f"site{number:02}.com", language="en")
        SitePrice.objects.create(
            site=site,
            seller=seller,
            placement_cents=10000,
            currency="EUR",
            source=MetricSource.MANUAL,
            checked_at=start_of_day(dt.date(2026, 10, 1)),
            reviewed_at=timezone.now(),
        )


def _open_filter(page: Page) -> Locator:
    """Меню продавцов свёрнуто: разворачиваем, иначе поле не нажать.

    Пункты ищем только в нём: на странице есть и другие фильтры с галочками
    («Страна площадки», ADR-082).
    """
    menu = page.locator(".seo-multifilter", has_text="Продавец")
    menu.locator("summary").click()
    expect(menu.locator(SEARCH)).to_be_visible()
    return menu


def test_search_narrows_the_list(admin_page: Page, live_server: LiveServer, sellers: None) -> None:
    """Критерий: поле сужает пункты на месте, «Все» остаётся."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    menu = _open_filter(page)

    before = menu.locator(ITEMS).count()
    assert before > 5, before

    menu.locator(SEARCH).fill("chaudhary")
    # Остаются «Все» и Chaudhary Adeel: скрытые действительно скрыты.
    expect(menu.locator(ITEMS)).to_have_count(2)
    expect(menu.locator(ITEMS, has_text="Chaudhary Adeel")).to_have_count(1)
    expect(menu.locator(".seo-multi-item", has_text="Athena Smith")).to_be_hidden()

    menu.locator(SEARCH).fill("")
    expect(menu.locator(ITEMS)).to_have_count(before)


def test_nothing_found_is_said(admin_page: Page, live_server: LiveServer, sellers: None) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    menu = _open_filter(page)
    menu.locator(SEARCH).fill("такого продавца нет")
    expect(menu.locator(".seo-multi-empty")).to_be_visible()
    expect(menu.locator(ITEMS)).to_have_count(1)  # только «Все»
