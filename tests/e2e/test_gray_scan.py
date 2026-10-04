"""Серость в Google в карточке площадки (E2-06) — seo/gray-scan.js и разбор числа расширением.

Числа вписывают руками или присылает расширение браузера сообщением окна —
его здесь имитирует `window.postMessage`: настоящий Google проверить нельзя
(запрос не из браузера человека встречает капча). Серверную часть проверяет
tests/test_gray_scan.py.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import GrayScan, Product, Site, SiteList, SiteListItem, SiteMetric

from .conftest import mark, same_document

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"
COUNT_JS = Path(settings.BASE_DIR) / "browser-extension" / "count.js"


@pytest.fixture
def site() -> Site:
    """youengage.me в рабочем списке Convertio."""
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site = Site.objects.create(domain="youengage.me", language="en")
    SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
    SiteListItem.objects.create(site_list=SiteList.objects.create(name="Октябрь"), site=site)
    return site


def _open_card(page: Page, live_server: LiveServer) -> None:
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    mark(page)
    page.locator("#result_list a[data-panel]", has_text="youengage.me").click()
    expect(page.locator(PANEL).locator(".seo-panel-title")).to_have_text("youengage.me")
    # Заголовок панели виден раньше, чем выполнены скрипты карточки.
    page.wait_for_function("window.seoGrayReady === true")


def _send(page: Page, query: str, count: int | None, urls: list[str] | None = None) -> None:
    """Сообщение, которое шлёт расширение (browser-extension/relay.js)."""
    page.evaluate(
        "([query, count, urls]) => window.postMessage("
        "{type: 'seo-gray-reading', query, count, urls}, location.origin)",
        [query, count, urls or []],
    )


def test_typed_numbers(admin_page: Page, live_server: LiveServer, site: Site) -> None:
    """Вписал два числа → доля и зона сразу → «Сохранить» → замер, строка списка обновилась."""
    page = admin_page
    _open_card(page, live_server)
    section = page.locator(PANEL).locator("[data-gray-scan]")
    expect(section.locator("a.seo-btn", has_text="Google: всего страниц")).to_have_attribute(
        "href", "https://www.google.com/search?q=site%3Ayouengage.me&hl=en"
    )
    section.locator("input[name=total]").fill("375")
    section.locator("input[name=gray]").fill("12")
    expect(section.locator("[data-gray-result]")).to_have_text("→ 3,2 % · зелёная зона")
    section.locator("input[name=gray]").fill("100")
    expect(section.locator("[data-gray-result]")).to_have_text("→ 26,67 % · красная зона")
    section.locator("button[type=submit]").click()

    latest = page.locator(PANEL).locator(".seo-gray-latest")
    expect(latest).to_contain_text("26,67 %")
    scan = GrayScan.objects.get(site=site)
    assert (scan.total_indexed, scan.gray_hits, scan.ratio) == (375, 100, Decimal("26.67"))
    assert (scan.breakdown or {})["source"] == "typed"

    page.keyboard.press("Escape")
    expect(page.locator(PANEL)).to_be_hidden()
    expect(page.locator("#result_list .seo-gray-zone.seo-gray-red")).to_have_text("26,67 %")
    assert same_document(page)


def test_numbers_from_extension(admin_page: Page, live_server: LiveServer, site: Site) -> None:
    """Расширение прислало оба числа → поля заполнены, замер сохранился сам, с примерами."""
    page = admin_page
    _open_card(page, live_server)
    section = page.locator(PANEL).locator("[data-gray-scan]")
    gray_query = section.get_attribute("data-gray-query")
    assert gray_query and gray_query.startswith('site:youengage.me "casino" OR "poker"')

    _send(page, "site:other.com", 999)
    _send(page, "site:youengage.me", 375)
    total = section.locator("input[name=total]")
    expect(total).to_have_value("375")
    expect(total).to_have_class("seo-gray-filled")
    expect(section.locator("[data-gray-hint]")).to_contain_text("Теперь «Google: серые темы»")
    assert not GrayScan.objects.exists()

    _send(page, gray_query, 12, ["https://youengage.me/casino-review", "https://youengage.me/cbd"])
    expect(page.locator(PANEL).locator(".seo-gray-latest")).to_contain_text("3,2 %")
    scan = GrayScan.objects.get(site=site)
    assert (scan.total_indexed, scan.gray_hits) == (375, 12)
    assert (scan.breakdown or {})["source"] == "extension"
    assert (scan.breakdown or {})["queries"] == {"total": "site:youengage.me", "gray": gray_query}
    assert scan.sample_urls == ["https://youengage.me/casino-review", "https://youengage.me/cbd"]


def test_typed_correction_stops_auto_save(
    admin_page: Page, live_server: LiveServer, site: Site
) -> None:
    """Поправил число от расширения руками — второе число замер само не сохраняет."""
    page = admin_page
    _open_card(page, live_server)
    section = page.locator(PANEL).locator("[data-gray-scan]")
    _send(page, "site:youengage.me", 375)
    section.locator("input[name=total]").fill("380")
    _send(page, section.get_attribute("data-gray-query") or "", 12)
    expect(section.locator("input[name=gray]")).to_have_value("12")
    expect(section.locator("[data-gray-result]")).to_have_text("→ 3,16 % · зелёная зона")
    assert not GrayScan.objects.exists()


def test_extension_without_count(admin_page: Page, live_server: LiveServer, site: Site) -> None:
    """Google не показал оценку → подсказка вписать руками, поле пустое."""
    page = admin_page
    _open_card(page, live_server)
    section = page.locator(PANEL).locator("[data-gray-scan]")
    _send(page, "site:youengage.me", None)
    expect(section.locator("[data-gray-hint]")).to_contain_text("впишите число руками")
    expect(section.locator("input[name=total]")).to_have_value("")


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("About 1,230 results (0.32 seconds)", 1230),
        ("About 12,900,000 results (0.41 seconds) ", 12900000),
        ("1 result (0.20 seconds)", 1),
        ("Page 2 of about 1,230 results (0.30 seconds)", 1230),
        ("Результатов: примерно 1 230 (0,32 сек.)", 1230),
        ("About 0 results", 0),
        ("No results", None),
    ],
)
def test_count_from_google_line(page: Page, text: str, count: int | None) -> None:
    """Разбор строки «About N results» расширением — browser-extension/count.js."""
    page.set_content("<html><body></body></html>")
    page.add_script_tag(path=str(COUNT_JS))
    assert page.evaluate("text => seoGrayCount(text)", text) == count
