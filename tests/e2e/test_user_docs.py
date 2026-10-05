"""Раздел «Документация» в интерфейсе (E9-07, ADR-056) — в браузере.

Сайт документации собирается здесь же из текущих user-docs/ — так же, как в
образе: `mkdocs build`, затем карты проверщика. Не из образа: проверки не
зависят от того, пересобирали ли его после правки страниц. Отдачу, карту
экранов и «Что нового» без браузера проверяет tests/test_user_docs.py.
"""

import re
import subprocess
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import FloatRect, Page, expect
from pytest_django import Settings
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Site, SiteList, SiteListItem
from tools.check_user_docs import main as check_docs

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path("test-results/e2e")
FIND_SITES = "Как найти площадку и отфильтровать список"
# Верх экрана со списком: заголовок с «?» и открытое меню.
TOP: FloatRect = {"x": 0, "y": 0, "width": 1920, "height": 520}


@pytest.fixture(scope="session")
def built_docs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site = tmp_path_factory.mktemp("user-docs")
    subprocess.run(["mkdocs", "build", "--quiet", "--site-dir", str(site)], cwd=ROOT, check=True)
    maps = ["--screens-out", str(site / "screens.json")]
    assert check_docs([*maps, "--whatsnew-out", str(site / "whatsnew.json")]) == 0
    return site


@pytest.fixture(autouse=True)
def docs_root(built_docs: Path, settings: Settings) -> None:
    settings.USER_DOCS_ROOT = built_docs


@pytest.fixture
def listed() -> Site:
    """known.com в рабочем списке Convertio — строка в «Площадках»."""
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site = Site.objects.create(domain="known.com", language="en")
    SiteListItem.objects.create(site_list=SiteList.objects.create(name="Октябрь"), site=site)
    return site


def test_help_on_site_list_opens_page(
    admin_page: Page, live_server: LiveServer, listed: Site
) -> None:
    """«?» у «Площадок» — меню, первая — страница только про них, в новой вкладке."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    menu = page.locator("#content h1 [data-seo-help]")
    items = menu.locator(".seo-help-list a")
    expect(items.first).to_be_hidden()
    menu.locator("button").click()
    expect(items.first).to_have_text(FIND_SITES)
    assert items.count() > 1

    with page.context.expect_page() as opened:
        items.first.click()
    docs = opened.value
    docs.wait_for_load_state()
    assert docs.url == f"{live_server.url}/docs/how-to/find-sites/"
    expect(docs.locator(".md-content h1")).to_contain_text(FIND_SITES)
    # Рабочий экран на месте, меню закрылось.
    assert page.url == f"{live_server.url}/admin/sites/productsitelatest/"
    expect(items.first).to_be_hidden()


def test_docs_search_in_russian(admin_page: Page, live_server: LiveServer) -> None:
    """Поиск по-русски: «индексация» находит инструкцию по проверке индексации."""
    page = admin_page
    page.goto(f"{live_server.url}/docs/")
    search = page.locator(".md-search__input")
    search.click()
    # Указатель поиска грузится в фоне; запрос, набранный раньше, теряется.
    # Пока грузится — «Инициализация поиска», готов — приглашение печатать.
    expect(page.locator(".md-search-result__meta")).to_contain_text("Начните печатать")
    # Кириллицу Playwright вставляет без нажатий клавиш, а поиск Material
    # срабатывает на отпускание клавиши: End — как последняя клавиша человека.
    search.fill("индексация")
    search.press("End")
    result = page.locator(".md-search-result__link[href*='how-to/check-indexation/']").first
    expect(result).to_be_visible(timeout=10_000)
    result.click()
    expect(page).to_have_url(re.compile(r"/docs/how-to/check-indexation/"))


def test_docs_after_login_back_to_page(
    page: Page, live_server: LiveServer, admin_user: User
) -> None:
    """Без входа — страница входа; после входа — та страница, что открывали."""
    page.goto(f"{live_server.url}/docs/how-to/find-sites/")
    expect(page).to_have_url(re.compile(r"/admin/login/\?next=/docs/how-to/find-sites/"))
    page.fill("#id_username", "admin")
    page.fill("#id_password", "password")
    page.locator("input[type=submit], button[type=submit]").first.click()
    expect(page).to_have_url(f"{live_server.url}/docs/how-to/find-sites/")
    expect(page.locator(".md-content h1")).to_contain_text(FIND_SITES)


def test_header_link_opens_docs(admin_page: Page, live_server: LiveServer) -> None:
    """«Документация» в шапке — главная раздела в новой вкладке; логотип — обратно."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    with page.context.expect_page() as opened:
        page.locator("#user-tools a", has_text="Документация").click()
    docs = opened.value
    docs.wait_for_load_state()
    assert docs.url == f"{live_server.url}/docs/"
    expect(docs.locator(".md-content h1")).to_contain_text("Документация")
    docs.locator(".md-header__button.md-logo").click()
    expect(docs).to_have_url(f"{live_server.url}/admin/")


def test_help_in_panel_closes_first(
    admin_page: Page, live_server: LiveServer, listed: Site
) -> None:
    """В панели карточки «?» — меню; Esc закрывает меню, второй — панель."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("#result_list a[data-panel]", has_text="known.com").click()
    panel = page.locator(".seo-panel")
    expect(panel.locator(".seo-panel-title")).to_have_text("known.com")
    menu = panel.locator(".seo-panel-tools [data-seo-help]")
    menu.locator("button").click()
    expect(menu.locator(".seo-help-list")).to_be_visible()
    # Меню в панели не уходит за её правый край.
    box = menu.locator(".seo-help-list").bounding_box()
    viewport = page.viewport_size
    assert box is not None and viewport is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]

    page.keyboard.press("Escape")
    expect(menu.locator(".seo-help-list")).to_be_hidden()
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


@pytest.mark.parametrize("palette", ["apple-light", "google-dark", "emerald", "ahrefs"])
def test_help_and_news_look(
    admin_page: Page, live_server: LiveServer, listed: Site, palette: str
) -> None:
    """Снимки «?» с меню и «Что нового» на главной в расцветке — для глаз."""
    page = admin_page
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(f"{live_server.url}/admin/")
    page.evaluate(f"localStorage.setItem('admin-palette', '{palette}')")
    page.goto(f"{live_server.url}/admin/")
    news = page.locator(".home-news")
    expect(news).to_contain_text("Что нового")
    expect(news).to_contain_text("ещё не на рабочем сервере")
    expect(news.locator(".home-news-body li").first).to_be_visible()
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=SHOTS / f"docs-home-{palette}.png")

    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("#content h1 [data-seo-help] button").click()
    page.screenshot(path=SHOTS / f"docs-help-{palette}.png", clip=TOP)
