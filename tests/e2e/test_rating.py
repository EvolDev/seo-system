"""Оценка площадки звёздочкой (E1-21) — seo/rating.js.

Окошко открывается у звезды, наведение подсвечивает звёзды, щелчок ставит
оценку и меняет число на месте, без перезагрузки. Крестик снимает оценку.
Серверную часть проверяет tests/test_site_rating.py.
"""

import re

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Site, SiteRating

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

SITES = "/admin/sites/productsitelatest/"
PICK = ".seo-rating-pick"
EMPTY = re.compile(r"\bseo-rating-empty\b")


@pytest.fixture
def site() -> Site:
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    return Site.objects.create(domain="known.com", language="en")


def _star(page: Page) -> Locator:
    return page.locator("#result_list .seo-rating").first


def test_picker_sets_the_rating_without_reload(
    admin_page: Page, live_server: LiveServer, site: Site
) -> None:
    """Критерий: щелчок по звезде в окошке ставит оценку и меняет число на месте."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    star = _star(page)
    expect(star).to_have_class(EMPTY)

    star.click()
    expect(page.locator(PICK)).to_be_visible()

    with page.expect_response(lambda response: "/rate/" in response.url):
        page.locator(f"{PICK} [data-value='4']").click()

    expect(page.locator(PICK)).to_have_count(0)
    expect(star.locator(".seo-rating-value")).to_have_text("4.0")
    assert SiteRating.objects.get(site=site).value == 4

    # Страница не перезагружалась: число пришло ответом, а не новым экраном.
    expect(star).to_have_attribute("data-mine", "4")


def test_picker_shows_my_rating_and_clears_it(
    admin_page: Page, live_server: LiveServer, site: Site
) -> None:
    """Критерий: окошко подсвечивает мою оценку, крестик её снимает."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    star = _star(page)
    star.click()
    with page.expect_response(lambda response: "/rate/" in response.url):
        page.locator(f"{PICK} [data-value='3']").click()
    expect(star.locator(".seo-rating-value")).to_have_text("3.0")

    star.click()
    lit = page.locator(f"{PICK} .seo-rating-star.is-on")
    expect(lit).to_have_count(3)

    with page.expect_response(lambda response: "/rate/" in response.url):
        page.locator(f"{PICK} .seo-rating-clear").click()
    expect(star.locator(".seo-rating-value")).to_have_count(0)
    expect(star).to_have_class(EMPTY)
    assert not SiteRating.objects.exists()


def test_escape_closes_the_picker(admin_page: Page, live_server: LiveServer, site: Site) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    _star(page).click()
    expect(page.locator(PICK)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(PICK)).to_have_count(0)
    assert not SiteRating.objects.exists()
