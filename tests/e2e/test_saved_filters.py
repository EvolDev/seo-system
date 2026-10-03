"""«Мои фильтры» на «Площадках» (E9-10, ADR-050): критерии приёмки в браузере.

Набор сохраняется, применяется одним выбором без перезагрузки, удаляется и
возвращается «Отменить»; «Как в прошлый раз» возвращает фильтры после
закрытия вкладки.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Site, SiteList, SiteListItem, SiteMetric
from apps.workspace.models import SavedFilter

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

SITES = "/admin/sites/productsitelatest/"
NAME = "Немецкие DR 10+"


@pytest.fixture
def sites() -> None:
    """30 площадок в рабочем списке: DR — номер, каждая третья — на немецком."""
    Product.objects.create(name="Convertio", domain="convertio.co")
    site_list = SiteList.objects.create(name="Октябрь")
    for number in range(30):
        language = "de" if number % 3 == 0 else "en"
        site = Site.objects.create(domain=f"site{number:02}.com", language=language)
        SiteMetric.objects.create(site=site, dr=number, organic_traffic=number * 10)
        SiteListItem.objects.create(site_list=site_list, site=site)


def _rows(page: Page) -> int:
    return page.locator("#result_list tbody tr").count()


def _select(page: Page) -> str:
    return str(page.locator(".seo-saved-select").evaluate("s => s.selectedOptions[0].textContent"))


def _filter(page: Page) -> None:
    """Язык «de» и DR от 10: площадки 12, 15 … 27."""
    with soft_load(page):
        dropdown = page.locator(".list-filter-dropdown").filter(has_text="язык")
        dropdown.locator("select").select_option(label="de")
    with soft_load(page):
        page.locator("#id_dr__range__gte").fill("10")
        page.locator("#id_dr__range__gte").press("Enter")
    assert _rows(page) == 6


def test_save_apply_delete_undo(admin_page: Page, live_server: LiveServer, sites: None) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    mark(page)
    block = page.locator(".seo-saved")
    expect(block).to_contain_text("Мои фильтры")
    expect(block.locator("[data-saved-delete]")).to_be_disabled()
    _filter(page)

    # «Добавить» — поле названия в блоке, «Сохранить» — набор выбран, сообщение внизу.
    block.locator("[data-saved-add]").click()
    block.locator(".seo-saved-form input[name=name]").fill(NAME)
    block.locator(".seo-saved-form [type=submit]").click()
    expect(page.locator(".seo-toast").last).to_contain_text(f"Набор «{NAME}» сохранён.")
    expect(block.locator(".seo-saved-form")).to_be_hidden()
    assert _select(page) == NAME
    expect(block.locator("[data-saved-delete]")).to_be_enabled()
    saved = SavedFilter.objects.get()
    assert saved.query == "language=de&dr__range__gte=10"

    # Тот же набор ещё раз — вопрос и «Заменить», а не второй набор.
    block.locator("[data-saved-add]").click()
    block.locator(".seo-saved-form input[name=name]").fill(NAME.lower())
    block.locator(".seo-saved-form [type=submit]").click()
    expect(block.locator(".seo-saved-note")).to_contain_text("уже есть")
    expect(block.locator(".seo-saved-form [type=submit]")).to_have_text("Заменить")
    block.locator("[data-saved-cancel]").click()
    assert SavedFilter.objects.count() == 1

    # Список без фильтров (по меню) — набор одним выбором, без перезагрузки.
    with soft_load(page):
        page.locator("#nav-sidebar a", has_text="Площадки").click()
    assert _rows(page) == 30
    assert _select(page) == "Набор не выбран"
    with soft_load(page):
        page.locator(".seo-saved-select").select_option(label=NAME)
    assert "language=de" in page.url and "dr__range__gte=10" in page.url
    assert _rows(page) == 6
    assert _select(page) == NAME
    assert same_document(page)

    # «Удалить» — набор пропал, в сообщении «Отменить»; набор помечен, не стёрт.
    block.locator("[data-saved-delete]").click()
    toast = page.locator(".seo-toast", has_text=f"Набор «{NAME}» удалён.")
    expect(toast).to_be_visible()
    expect(block.locator("optgroup option")).to_have_count(0)
    assert _select(page) == "Набор не выбран"
    saved.refresh_from_db()
    assert saved.deleted_at is not None

    # «Отменить» — набор вернулся и снова выбран: фильтры на экране — его.
    toast.locator(".seo-toast-action", has_text="Отменить").click()
    expect(page.locator(".seo-toast").last).to_contain_text(f"Набор «{NAME}» возвращён.")
    expect(block.locator("optgroup option")).to_have_count(1)
    assert _select(page) == NAME
    saved.refresh_from_db()
    assert saved.deleted_at is None
    assert same_document(page)


def test_last_time_after_tab_closed(admin_page: Page, live_server: LiveServer, sites: None) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    # Ни разу не открывали с фильтрами — «Как в прошлый раз» выбрать нельзя.
    expect(page.locator(".seo-saved-select option[data-last]")).to_be_disabled()
    _filter(page)
    context = page.context
    page.close()

    # Новая вкладка того же браузера, список — из меню, без фильтров.
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.goto(f"{live_server.url}/admin/")
    with soft_load(tab):
        tab.locator("a.home-card", has_text="Площадки").click()
    assert _rows(tab) == 30
    mark(tab)
    with soft_load(tab):
        tab.locator(".seo-saved-select").select_option(label="Как в прошлый раз")
    assert "language=de" in tab.url and "dr__range__gte=10" in tab.url
    assert _rows(tab) == 6
    assert same_document(tab)
    assert not errors, errors
