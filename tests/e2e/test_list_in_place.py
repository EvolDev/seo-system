"""Список обновляется на месте (E9-12): введённое не затирается, экран не пересоздаётся.

Признак «на месте» — элемент страницы пережил переход тем же объектом:
перед переходом он запоминается в `window`, после — сравнивается с тем, что
на странице сейчас.
"""

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import (
    Product,
    Seller,
    Site,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
)

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

SITES = "/admin/sites/productsitelatest/"


@pytest.fixture
def sites() -> None:
    """150 площадок в рабочем списке: две страницы; каждая третья — на немецком.

    У первых 60 — трафик США: в фильтрах есть «Регион».
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


def _hold(page: Page, name: str, selector: str) -> None:
    page.evaluate(f"window.__held_{name} = document.querySelector({selector!r})")


def _same(page: Page, name: str, selector: str) -> bool:
    return bool(page.evaluate(f"window.__held_{name} === document.querySelector({selector!r})"))


def _rows(page: Page) -> int:
    return page.locator("#result_list tbody tr").count()


def _language(page: Page, label: str) -> None:
    dropdown = page.locator(".list-filter-dropdown").filter(has_text="язык")
    dropdown.locator("select").select_option(label=label)


def test_filter_keeps_typed_and_chosen(
    admin_page: Page, live_server: LiveServer, sites: None
) -> None:
    """Критерий E9-12: фильтр, сортировка и страница не трогают поиск, «Действие», «продавца»."""
    page = admin_page
    seller = Seller.objects.order_by("pk").first()
    assert seller is not None
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    _hold(page, "search", "#searchbar")
    _hold(page, "filters", "#changelist-filter")

    # Набрано, но «Найти» не нажата; выбраны действие и продавец; «от» DR не отправлено.
    page.locator("#searchbar").fill("site01")
    page.locator("select[name=action]").select_option("fix_seller_action")
    page.locator("select[name=seller]").select_option(str(seller.pk))
    page.locator("#id_organic_traffic__range__gte").fill("700")

    with soft_load(page):
        _language(page, "de")
    assert "language=de" in page.url and "q=" not in page.url
    assert _rows(page) == 50  # по ненажатому поиску не отбирается
    expect(page.locator("#searchbar")).to_have_value("site01")
    expect(page.locator("select[name=action]")).to_have_value("fix_seller_action")
    expect(page.locator("select[name=seller]")).to_have_value(str(seller.pk))
    expect(page.locator("#id_organic_traffic__range__gte")).to_have_value("700")
    # Поле поиска и колонка фильтров — те же элементы, а не новые.
    assert _same(page, "search", "#searchbar")
    assert _same(page, "filters", "#changelist-filter")
    # Поиск по-прежнему уходит с новым фильтром: скрытые поля формы поиска обновлены.
    expect(page.locator("#changelist-search input[name=language]")).to_have_value("de")

    with soft_load(page):
        page.locator("#result_list thead th.column-dr_cell .text a").dispatch_event("click")
    assert "o=" in page.url
    expect(page.locator("#searchbar")).to_have_value("site01")
    expect(page.locator("select[name=action]")).to_have_value("fix_seller_action")

    # «Найти» — теперь отбирает; затем «всего» (сбросить поиск) очищает поле.
    with soft_load(page):
        page.locator("#searchbar").press("Enter")
    assert "q=site01" in page.url
    assert _rows(page) == 3  # site012, 015, 018 — немецкие
    with soft_load(page):
        page.locator("#changelist-search .small.quiet a").click()
    assert "q=" not in page.url
    expect(page.locator("#searchbar")).to_have_value("")
    assert same_document(page)


def test_actions_work_after_refresh(admin_page: Page, live_server: LiveServer, sites: None) -> None:
    """Строки новые — отметки, «Выбрано N» и сам выбор страниц работают как раньше."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    # Сортировка — первая правка на месте, строки уже не те, что при загрузке.
    with soft_load(page):
        page.locator("#result_list thead th.column-domain_link .text a").dispatch_event("click")
    assert "o=" in page.url
    assert _rows(page) == 100
    page.locator("#result_list tbody input.action-select").nth(0).check()
    page.locator("#result_list tbody input.action-select").nth(1).check()
    expect(page.locator(".action-counter")).to_contain_text("2")
    page.locator("#action-toggle").check()
    expect(page.locator(".action-counter")).to_contain_text("100")

    # Следующая страница — отметки сброшены (строки другие), счётчик с нуля.
    with soft_load(page):
        page.locator(".paginator a", has_text="2").first.dispatch_event("click")
    assert "p=2" in page.url
    expect(page.locator("#result_list tbody input.action-select:checked")).to_have_count(0)
    page.locator("#result_list tbody input.action-select").nth(0).check()
    expect(page.locator(".action-counter")).to_contain_text("1")
    assert same_document(page)


def test_region_picker_after_refresh(
    admin_page: Page, live_server: LiveServer, sites: None
) -> None:
    """Выбор страны после правки на месте: один, рабочий, с уже выбранными фильтрами."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    with soft_load(page):
        _language(page, "de")
    expect(page.locator(".seo-region-filter .seo-country-input")).to_have_count(1)
    with soft_load(page):
        page.locator(".seo-region-filter .seo-country-input").fill("us")
        page.locator(".seo-region-filter li[role=option]").first.dispatch_event("mousedown")
    assert "region=us" in page.url and "language=de" in page.url
    expect(page.locator(".seo-region-filter .seo-country-input")).to_have_count(1)
    expect(page.locator("#changelist-filter")).to_contain_text("Трафик US")
    assert same_document(page)


def test_no_fade_and_dim_while_loading(
    admin_page: Page, live_server: LiveServer, sites: None
) -> None:
    """Экран не гаснет целиком (нет View Transition); строки бледнеют, пока идёт запрос."""
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    page.evaluate(
        "window.__transitions = 0;"
        " const start = document.startViewTransition.bind(document);"
        " document.startViewTransition = (cb) => { window.__transitions += 1; return start(cb); };"
        # Строка, которая кончается функцией, Playwright бы вызвал — кончаем числом.
        " 0"
    )
    # Ответ сервера держим, чтобы застать бледность.
    held: list[Route] = []
    page.route(lambda url: "language=" in url, lambda route: held.append(route))
    _language(page, "de")
    expect(page.locator("#changelist-form.seo-list-loading")).to_have_count(1)
    page.wait_for_function("true")  # дать Playwright принять запрос
    assert held
    held[0].continue_()
    expect(page.locator("#changelist-form.seo-list-loading")).to_have_count(0, timeout=10_000)
    assert _rows(page) == 50
    assert page.evaluate("window.__transitions") == 0
