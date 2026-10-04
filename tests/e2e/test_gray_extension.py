"""Расширение браузера целиком (E2-06): Google → фоновая часть → карточка площадки.

Chromium с загруженным `browser-extension/`; страницы www.google.com подменяет
перехват запросов Playwright — настоящий Google встретил бы капчей. Подставная
выдача повторяет то, что читает расширение: оценка в `#result-stats` (у серых
появляется только после «Tools»), результаты — ссылки с `<h3>`.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from playwright.sync_api import BrowserContext, Playwright, Route, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import GrayScan, Product, Site, SiteList, SiteListItem, SiteMetric

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

EXTENSION = Path(settings.BASE_DIR) / "browser-extension"
PANEL = ".seo-panel"

TOTAL_PAGE = """<html><body>
<div id="result-stats">About 375 results<nobr> (0.21 seconds)</nobr></div>
<div id="search"><div id="rso"><a href="https://youengage.me/"><h3>Home</h3></a></div></div>
</body></html>"""

# Оценка спрятана: появляется после нажатия «Tools», как у Google.
GRAY_PAGE = """<html><body>
<div id="hdtb"><div role="button" id="tools">Tools</div></div>
<div id="search"><div id="rso">
  <div><a href="https://youengage.me/casino-review"><h3>Casino review</h3></a></div>
  <div><a href="https://www.google.com/url?q=https://youengage.me/cbd&amp;sa=U"><h3>CBD</h3></a></div>
  <div><a href="https://youengage.me/casino-review"><h3>Casino review again</h3></a></div>
</div></div>
<script>
document.getElementById("tools").addEventListener("click", function () {
  setTimeout(function () {
    var stats = document.createElement("div");
    stats.id = "result-stats";
    stats.textContent = "About 12 results (0.30 seconds)";
    document.body.appendChild(stats);
  }, 300);
});
</script>
</body></html>"""


@pytest.fixture
def site() -> Site:
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site = Site.objects.create(domain="youengage.me", language="en")
    SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
    SiteListItem.objects.create(site_list=SiteList.objects.create(name="Октябрь"), site=site)
    return site


@pytest.fixture
def browser_with_extension(
    playwright: Playwright, tmp_path: Path, live_server: LiveServer, admin_user: User
) -> Iterator[BrowserContext]:
    # Расширения работают только в постоянном профиле и в полном Chromium
    # (channel="chromium" — новый режим без окна).
    context = playwright.chromium.launch_persistent_context(
        str(tmp_path / "profile"),
        channel="chromium",
        headless=True,
        args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
        # У пользователя образа нет домашней папки, а полному Chromium она нужна
        # (обработчик сбоев пишет туда базу) — без неё браузер падает на запуске.
        env={**os.environ, "HOME": str(tmp_path)},
    )
    client = Client()
    client.force_login(admin_user)
    session = client.cookies[settings.SESSION_COOKIE_NAME].value
    context.add_cookies(
        [{"name": settings.SESSION_COOKIE_NAME, "value": session, "url": live_server.url}]
    )

    def google(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)["q"][0]
        body = TOTAL_PAGE if query == "site:youengage.me" else GRAY_PAGE
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    context.route("https://www.google.com/search*", google)
    yield context
    context.close()


def test_extension_fills_card_and_saves(
    browser_with_extension: BrowserContext, live_server: LiveServer, site: Site
) -> None:
    context = browser_with_extension
    page = context.new_page()
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("#result_list a[data-panel]", has_text="youengage.me").click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-title")).to_have_text("youengage.me")
    page.wait_for_function("window.seoGrayReady === true")
    section = panel.locator("[data-gray-scan]")

    with context.expect_page() as opened:
        section.locator("a.seo-btn", has_text="Google: всего страниц").click()
    opened.value.wait_for_load_state()
    expect(section.locator("input[name=total]")).to_have_value("375", timeout=10_000)
    assert not GrayScan.objects.exists()

    with context.expect_page() as opened:
        section.locator("a.seo-btn", has_text="Google: серые темы").click()
    google = opened.value
    expect(google.locator("#result-stats")).to_have_text(
        "About 12 results (0.30 seconds)", timeout=10_000
    )
    expect(google.get_by_text("SEO-система: 12 — в карточку площадки")).to_be_visible()

    expect(panel.locator(".seo-gray-latest")).to_contain_text("3,2 %", timeout=10_000)
    scan = GrayScan.objects.get(site=site)
    assert (scan.total_indexed, scan.gray_hits) == (375, 12)
    assert (scan.breakdown or {})["source"] == "extension"
    assert scan.sample_urls == ["https://youengage.me/casino-review", "https://youengage.me/cbd"]


def test_ordinary_search_is_left_alone(
    browser_with_extension: BrowserContext, live_server: LiveServer, site: Site
) -> None:
    """Поиск, которого не ждёт ни одна карточка, расширение не трогает: «Tools» не нажат."""
    context = browser_with_extension
    google = context.new_page()
    google.goto("https://www.google.com/search?q=site%3Aother.com+%22casino%22&hl=en")
    google.wait_for_timeout(1500)
    expect(google.locator("#result-stats")).to_have_count(0)
    expect(google.get_by_text("SEO-система")).to_have_count(0)
