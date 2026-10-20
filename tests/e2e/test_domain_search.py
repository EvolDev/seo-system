"""Поиск столбиком доменов (seo/domain-search.js, admin/search_form.html).

Вставили столбик — поле раскрылось; Enter — в списке размещения этих доменов
без перезагрузки страницы, рядом со счётчиком — домены, которых нет.
Shift+Enter — новая строка, а не поиск.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement
from apps.sites.models import Product, Site

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PLACEMENTS = "/admin/placements/placement/"
FOUND = (
    "fashiongonerogue.com",
    "theworldonmynecklace.com",
    "psd-dude.com",
    "veed.io",
    "amasty.com",
)
# Столбик из просьбы пользователя (19.10.2026): трёх площадок в размещениях нет.
PASTED = "\n".join(
    (
        "fashiongonerogue.com",
        "theworldonmynecklace.com",
        "psd-dude.com",
        "lensa.com",
        "opencart.com",
        "trueconf.com",
        "www.veed.io",
        "amasty.com",
    )
)


@pytest.fixture
def placements() -> None:
    product, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    for name in (*FOUND, "valensa.com", "spacecoastdaily.com"):
        Placement.objects.create(site=Site.objects.create(domain=name), product=product)


def test_pasted_column_shows_these_domains(
    admin_page: Page, live_server: LiveServer, placements: None
) -> None:
    page = admin_page
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(f"{live_server.url}{PLACEMENTS}")
    box = page.locator("#searchbar")
    expect(box).to_have_attribute("rows", "1")
    page.locator("#toolbar").screenshot(path="test-results/domain-search-empty.png")

    mark(page)
    box.fill(PASTED)
    expect(box).to_have_attribute("rows", "8")
    with soft_load(page):
        box.press("Enter")
    assert same_document(page)

    expect(page.locator("#result_list tbody tr")).to_have_count(len(FOUND))
    expect(page.locator(".seo-dsearch-missing")).to_contain_text(
        "нет в списке (3): lensa.com, opencart.com, trueconf.com"
    )
    expect(page.locator("#searchbar")).to_have_attribute("rows", "8")
    page.screenshot(path="test-results/domain-search-1920.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.screenshot(path="test-results/domain-search-1440.png")


def test_shift_enter_adds_a_line(
    admin_page: Page, live_server: LiveServer, placements: None
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{PLACEMENTS}")
    box = page.locator("#searchbar")
    box.fill("lensa.com")
    box.press("Shift+Enter")
    box.press_sequentially("veed.io")
    expect(box).to_have_value("lensa.com\nveed.io")
    expect(box).to_have_attribute("rows", "2")
    assert "q=" not in page.url
