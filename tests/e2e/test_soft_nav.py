"""Админка без перезагрузки страниц (E9-09, ADR-046): переходы, «Назад»."""

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement
from apps.sites.models import (
    Product,
    Site,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
)

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

SITES = "/admin/sites/productsitelatest/"
PLACEMENTS = "/admin/placements/placement/"


@pytest.fixture
def sites() -> None:
    """Продукт и 150 площадок в рабочем списке: в «Площадках» две страницы.

    DR — номер по модулю 100; каждая третья — на немецком; у первых 60 есть
    трафик США (регион).
    """
    Product.objects.create(name="Convertio", domain="convertio.co")
    site_list = SiteList.objects.create(name="Октябрь")
    for number in range(150):
        language = "de" if number % 3 == 0 else "en"
        site = Site.objects.create(domain=f"site{number:03}.com", language=language)
        SiteMetric.objects.create(site=site, dr=number % 100, organic_traffic=number * 10)
        SiteListItem.objects.create(site_list=site_list, site=site)
        if number < 60:
            SiteCountryMetric.objects.create(site=site, country="us", organic_traffic=number)


def test_admin_opens(admin_page: Page, live_server: LiveServer) -> None:
    admin_page.goto(f"{live_server.url}/admin/")
    expect(admin_page.locator("#site-name")).to_contain_text("SEO-система")
    expect(admin_page.locator("html")).to_have_attribute("data-soft-nav", "on")


def test_screens_switch_without_reload(
    admin_page: Page, live_server: LiveServer, sites: None
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)

    # С главной (меню нет) — на «Площадки» (меню есть): меняется весь #main.
    with soft_load(page):
        page.locator("a.home-card", has_text="Площадки").click()
    expect(page).to_have_url(f"{live_server.url}{SITES}")
    expect(page.locator("#content h1")).to_have_text("Площадки")
    expect(page.locator("#nav-sidebar tr.current-model")).to_contain_text("Площадки")
    expect(page).to_have_title("Площадки | SEO-система")

    # По меню — на «Размещения»: меню остаётся, подсветка переезжает.
    with soft_load(page):
        page.locator("#nav-sidebar a", has_text="Размещения").click()
    expect(page).to_have_url(f"{live_server.url}{PLACEMENTS}")
    expect(page.locator("#content h1")).to_have_text("Размещения")
    expect(page.locator("#nav-sidebar tr.current-model")).to_contain_text("Размещения")
    expect(page.locator(".breadcrumbs")).to_contain_text("Размещения")

    with soft_load(page):
        page.evaluate("history.back()")
    expect(page).to_have_url(f"{live_server.url}{SITES}")
    expect(page.locator("#content h1")).to_have_text("Площадки")

    # На главную по крошкам: меню пропадает, крошек нет.
    with soft_load(page):
        page.locator(".breadcrumbs a").first.click()
    expect(page).to_have_url(f"{live_server.url}/admin/")
    expect(page.locator("#nav-sidebar")).to_have_count(0)
    expect(page.locator(".breadcrumbs")).to_have_count(0)

    assert same_document(page)


def test_back_returns_to_same_place(admin_page: Page, live_server: LiveServer, sites: None) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    page.evaluate("window.scrollTo(0, 900)")
    assert page.evaluate("window.scrollY") == 900

    with soft_load(page):
        page.locator("#nav-sidebar a", has_text="Размещения").click()
    assert page.evaluate("window.scrollY") == 0

    with soft_load(page):
        page.evaluate("history.back()")
    expect(page).to_have_url(f"{live_server.url}{SITES}")
    assert page.evaluate("window.scrollY") == 900

    with soft_load(page):
        page.evaluate("history.forward()")
    expect(page).to_have_url(f"{live_server.url}{PLACEMENTS}")
    assert same_document(page)


@pytest.fixture
def placement() -> Placement:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="blog.example.com", language="en")
    return Placement.objects.create(site=site, product=product)


def _widgets(page: Page) -> dict[str, int]:
    """Сколько чего на форме: задвоенный запуск скрипта даст лишнее."""
    counts: dict[str, int] = page.evaluate(
        """() => ({
            dateTime: document.querySelectorAll('.vDateField, .vTimeField').length,
            shortcuts: document.querySelectorAll('#content-main .datetimeshortcuts').length,
            days: document.querySelectorAll('#content-main input[type=date]').length,
            today: document.querySelectorAll('#content-main [data-today]').length,
            autocomplete: document.querySelectorAll(
                'select.admin-autocomplete:not([name*=__prefix__])').length,
            select2: document.querySelectorAll('#content-main .select2-container').length,
            inlines: document.querySelectorAll('.inline-related:not(.empty-form)').length,
        })"""
    )
    return counts


def test_admin_scripts_work_after_soft_navigation(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    """Главная без jQuery → список → панель → полная форма → «Сохранить и продолжить».

    Штатные скрипты Django на каждом экране работают ровно один раз: галочки
    и счётчик, автодополнение, «Добавить ещё», окно «+», календарь (у формы
    позиции ключа: у размещения даты — днём без времени, E9-11).
    """
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)

    with soft_load(page):
        page.locator("a.home-card", has_text="Размещения").click()
    page.locator("input.action-select").first.check()
    expect(page.locator(".action-counter")).to_contain_text("1 из 1")
    expect(page.locator("#result_list tbody tr").first).to_have_class("selected")

    # Запись — панелью (E9-11), полная форма — «Открыть полностью», тоже подгрузкой.
    page.locator("#result_list a", has_text="blog.example.com").click()
    with soft_load(page):
        page.locator(".seo-panel a", has_text="Открыть полностью").click()
    expect(page.locator("#content h1")).to_contain_text("Размещение")

    for _ in range(2):  # открыта переходом, затем — после сохранения
        counts = _widgets(page)
        assert counts["today"] == counts["days"] == 2
        assert counts["select2"] == counts["autocomplete"] > 0
        page.locator(".add-row a").click()
        assert _widgets(page)["inlines"] == counts["inlines"] + 1

        # Автодополнение ищет.
        page.locator("#id_site + .select2-container").click()
        expect(page.locator(".select2-search__field")).to_be_visible()
        page.keyboard.press("Escape")

        # «+» у продукта — окно темы поверх страницы, а не новое окно браузера.
        page.locator("#add_id_product").click()
        expect(page.locator("#related-modal-iframe")).to_have_count(1)
        page.locator(".mfp-close").click()
        expect(page.locator("#related-modal-iframe")).to_have_count(0)

        with soft_load(page):
            page.locator("input[name=_continue]").click()
        expect(page.locator(".messagelist")).to_contain_text("успешно")

    # Календарь Django — у формы позиции ключа.
    for _ in range(2):  # открыта переходом, затем — после второго перехода
        with soft_load(page):
            page.evaluate("seoNav.visit('/admin/keywords/keywordposition/add/')")
        counts = _widgets(page)
        assert counts["shortcuts"] == counts["dateTime"] > 0
        page.locator(".datetimeshortcuts a[id^=calendarlink]").first.click()
        expect(page.locator(".calendarbox:visible")).to_have_count(1)
        page.keyboard.press("Escape")

    assert same_document(page)


def test_forms_without_reload(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    """Поиск, действие над отмеченными, сохранение с ошибкой и без."""
    page = admin_page
    page.goto(f"{live_server.url}{PLACEMENTS}")
    mark(page)

    with soft_load(page):
        page.locator("#searchbar").fill("blog")
        page.keyboard.press("Enter")
    expect(page).to_have_url(f"{live_server.url}{PLACEMENTS}?q=blog")
    expect(page.locator("#result_list tbody tr")).to_have_count(1)
    expect(page.locator("#searchbar")).to_have_value("blog")

    with soft_load(page):
        page.locator("#searchbar").fill("нет-такого")
        page.keyboard.press("Enter")
    expect(page.locator("#result_list")).to_have_count(0)

    # Действие над отмеченными: кнопка «Выполнить» уходит на сервер вместе с формой.
    with soft_load(page):
        page.locator("#nav-sidebar a", has_text="Размещения").click()
    page.locator("input.action-select").first.check()
    page.locator("select[name=action]").select_option("skip_checks_action")
    with soft_load(page):
        page.locator("#changelist-form button[name=index]").click()
    expect(page.locator(".messagelist")).to_contain_text("Убрано из проверок по расписанию: 1")
    placement.refresh_from_db()
    assert placement.skip_checks

    # Ошибка в полной форме: остаёмся на форме, новой записи в истории нет.
    page.locator("#result_list a", has_text="blog.example.com").click()
    with soft_load(page):
        page.locator(".seo-panel a", has_text="Открыть полностью").click()
    change_url = page.url
    history = page.evaluate("history.length")
    page.locator("#id_price_paid_cents").fill("не сумма")
    with soft_load(page):
        page.locator("input[name=_save]").click()
    expect(page.locator(".errornote")).to_be_visible()
    assert page.url == change_url
    assert page.evaluate("history.length") == history

    # Исправили — список с сообщением об успехе.
    page.locator("#id_price_paid_cents").fill("")
    with soft_load(page):
        page.locator("input[name=_save]").click()
    expect(page).to_have_url(f"{live_server.url}{PLACEMENTS}")
    expect(page.locator(".messagelist")).to_contain_text("успешно")
    assert same_document(page)


def test_logout_is_a_real_navigation(admin_page: Page, live_server: LiveServer) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)
    with page.expect_navigation():
        page.locator("#logout-form button").click()
    assert not same_document(page)


def _rows(page: Page) -> int:
    return page.locator("#result_list tbody tr").count()


def test_sites_filters_without_reload(
    admin_page: Page, live_server: LiveServer, sites: None
) -> None:
    """Фильтры, сортировка и страницы «Площадок» — без перезагрузки (критерий E9-09)."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    assert _rows(page) == 100
    page.evaluate("window.scrollTo(0, 300)")

    # «от — до»: Enter в поле — как «Найти»; прокрутка на месте.
    with soft_load(page):
        page.locator("#id_dr__range__gte").fill("50")
        page.locator("#id_dr__range__gte").press("Enter")
    assert "dr__range__gte=50" in page.url
    assert _rows(page) == 50
    assert page.evaluate("window.scrollY") == 300

    # Выпадающий фильтр — к уже выбранному «от — до».
    with soft_load(page):
        dropdown = page.locator(".list-filter-dropdown").filter(has_text="язык")
        dropdown.locator("select").select_option(label="de")
    assert "language=de" in page.url and "dr__range__gte=50" in page.url
    assert _rows(page) == 17  # 51, 54 … 99

    # «Сбросить» у «от — до»: язык остаётся.
    with soft_load(page):
        page.locator(".numericrangefilter input[type=reset]").first.click()
    assert "dr__range" not in page.url and "language=de" in page.url
    assert _rows(page) == 50

    # Регион — выбор страны с поиском; появляются колонки и «от — до» региона.
    with soft_load(page):
        page.locator(".seo-region-filter .seo-country-input").fill("us")
        page.locator(".seo-region-filter li[role=option]").first.dispatch_event("mousedown")
    assert "region=us" in page.url
    expect(page.locator("#changelist-filter")).to_contain_text("Трафик US")
    expect(page.locator(".seo-region-filter .seo-country-input")).to_have_value(re.compile("^США"))

    # Сортировка по колонке — прокрутка на месте. Сначала без фильтров — по меню.
    with soft_load(page):
        page.locator("#nav-sidebar a", has_text="Площадки").click()
    assert _rows(page) == 100
    page.evaluate("window.scrollTo(0, 300)")
    # Нажатие без прокрутки к заголовку: click() Playwright сначала прокрутил бы
    # к нему страницу, и проверять было бы нечего.
    with soft_load(page):
        page.locator("#result_list thead th.column-dr_cell .text a").dispatch_event("click")
    assert "o=" in page.url
    assert page.evaluate("window.scrollY") == 300

    # Следующая страница — к началу списка.
    with soft_load(page):
        page.locator(".paginator a", has_text="2").first.dispatch_event("click")
    assert "p=2" in page.url
    assert page.evaluate("window.scrollY") == 0

    # «Назад» — снова первая страница и прежнее место.
    with soft_load(page):
        page.evaluate("history.back()")
    assert "p=2" not in page.url
    assert page.evaluate("window.scrollY") == 300
    assert same_document(page)


def test_every_menu_screen_opens_softly(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    """Обход меню: каждый экран и его форма «Добавить» открываются подгрузкой.

    Ошибка JavaScript на любом из них роняет проверку (фикстура admin_page).
    """
    page = admin_page
    page.goto(f"{live_server.url}{PLACEMENTS}")
    mark(page)
    links = page.locator("#nav-sidebar th[scope=row] a")
    hrefs = [links.nth(i).get_attribute("href") for i in range(links.count())]
    assert len(hrefs) > 10
    for href in hrefs:
        # «Служебное» свёрнуто — нажатие без проверки, видна ли ссылка.
        with soft_load(page):
            page.locator(f'#nav-sidebar th[scope=row] a[href="{href}"]').dispatch_event("click")
        expect(page).to_have_url(f"{live_server.url}{href}")
        expect(page.locator("#content")).to_be_visible()
        add = page.locator(".object-tools a.addlink")
        if not add.count():
            continue
        # Списки с панелью (E9-11) открывают форму «Добавить» панелью справа.
        if "seo-panel-list" in (page.locator("body").get_attribute("class") or ""):
            add.first.click()
            expect(page.locator(".seo-panel form")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator(".seo-panel")).to_be_hidden()
        else:
            with soft_load(page):
                add.first.click()
            expect(page.locator("#content form")).to_be_visible()
    assert same_document(page)


def test_two_list_selector_after_soft_navigation(admin_page: Page, live_server: LiveServer) -> None:
    """Права пользователя — выбор из двух списков: ровно по одному на поле."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)
    with soft_load(page):
        page.locator("a", has_text="Пользователи").first.click()
    with soft_load(page):
        page.locator("#result_list a", has_text="admin").first.click()
    expect(page.locator(".selector")).to_have_count(2)  # группы и права
    assert same_document(page)


def test_expired_session_goes_to_login(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    """Сессия кончилась — обычный переход на вход, как без подгрузки."""
    page = admin_page
    page.goto(f"{live_server.url}{PLACEMENTS}")
    mark(page)
    page.context.clear_cookies()
    with page.expect_navigation():
        page.locator("#nav-sidebar a", has_text="Площадки").click()
    expect(page).to_have_url(re.compile(r"/admin/login/\?next="))
    assert not same_document(page)
