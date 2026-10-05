"""Значки у домена (E2-06) — seo/domain-tools.js: скопировать домен, открыть сайт.

Сайт площадки не открывается по-настоящему: его адрес подменён перехватом
запросов Playwright.
"""

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Site, SiteList, SiteListItem, SiteMetric

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"


@pytest.fixture
def site() -> Site:
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site = Site.objects.create(domain="youengage.me", language="en")
    SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
    SiteListItem.objects.create(site_list=SiteList.objects.create(name="Октябрь"), site=site)
    return site


@pytest.fixture
def three_sites() -> list[Site]:
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site_list = SiteList.objects.create(name="Октябрь")
    sites = []
    for domain in ("alpha.com", "beta.de", "gamma.co.uk"):
        site = Site.objects.create(domain=domain, language="en")
        SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
        SiteListItem.objects.create(site_list=site_list, site=site)
        sites.append(site)
    return sites


def _fake_site(route: Route) -> None:
    route.fulfill(status=200, content_type="text/html", body="<title>youengage</title>")


def test_copy_and_open_from_sites_list(
    admin_page: Page, live_server: LiveServer, site: Site
) -> None:
    page = admin_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    page.context.route("https://youengage.me/**", _fake_site)
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    row = page.locator("#result_list tr", has_text="youengage.me")

    copy = row.locator("button[data-copy]")
    copy.click()
    expect(copy).to_have_class("seo-icon-btn seo-copied")
    assert page.evaluate("navigator.clipboard.readText()") == "youengage.me"
    expect(page.locator(PANEL)).to_be_hidden()

    with page.context.expect_page() as opened:
        row.locator("a[target=_blank]").click()
    assert opened.value.url == "https://youengage.me/"
    expect(page.locator(PANEL)).to_be_hidden()


def test_copy_selected_domains_as_a_column(
    admin_page: Page, live_server: LiveServer, three_sites: list[Site]
) -> None:
    """«Скопировать домены»: отмеченные строки — столбиком, по одному на строке."""
    page = admin_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    for domain in ("alpha.com", "gamma.co.uk"):
        page.locator("#result_list tr", has_text=domain).locator("input.action-select").check()

    button = page.locator("[data-copy-domains]")
    button.click()
    expect(button).to_have_text("Скопировано")
    assert page.evaluate("navigator.clipboard.readText()") == "alpha.com\ngamma.co.uk"
    expect(page.locator(".seo-toast-success")).to_have_text("Скопировано: 2 домена")


def test_copy_domains_without_selection_asks_to_select(
    admin_page: Page, live_server: LiveServer, three_sites: list[Site]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("[data-copy-domains]").click()
    expect(page.locator(".seo-toast")).to_have_text("Отметьте строки — скопирую их домены")


def test_card_title_opens_site(admin_page: Page, live_server: LiveServer, site: Site) -> None:
    page = admin_page
    page.context.route("https://youengage.me/**", _fake_site)
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("#result_list a[data-panel]", has_text="youengage.me").click()
    title = page.locator(PANEL).locator(".seo-panel-title")
    expect(title).to_have_text("youengage.me")
    with page.context.expect_page() as opened:
        title.locator("a", has_text="youengage.me").click()
    assert opened.value.url == "https://youengage.me/"
    expect(title.locator("button[data-copy]")).to_have_attribute("data-copy", "youengage.me")
