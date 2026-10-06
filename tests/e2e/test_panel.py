"""Панель записи справа поверх списка (E9-11, ADR-048) — seo/panel.js.

Запись открывается панелью, список остаётся на месте; «Сохранить» закрывает
панель, строка меняется на месте; ошибка в форме — в панели, без перехода;
Ctrl — полная страница. Серверную часть проверяет tests/test_panel.py.
"""

import datetime as dt
import re

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.keywords.models import Keyword
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    MetricSource,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
)
from apps.sites.uploads.plan import start_of_day

from .conftest import mark, same_document

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"


@pytest.fixture
def known() -> Site:
    """known.com в рабочем списке Convertio, с замером и рабочей ценой."""
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site = Site.objects.create(domain="known.com", language="en")
    SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
    price = SitePrice.objects.create(
        site=site,
        seller=Seller.collaborator(),
        placement_cents=20000,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 9, 27)),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    SiteListItem.objects.create(site_list=SiteList.objects.create(name="Октябрь"), site=site)
    return site


def test_site_card_changes_row_in_place(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Заметка в карточке → закрыть → строка «Площадок» обновилась, список не перечитан."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    mark(page)
    expect(page.locator(".seo-notes")).to_have_count(0)

    page.locator("#result_list a[data-panel]", has_text="known.com").click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-title")).to_have_text("known.com")
    expect(page.locator("#result_list tr.seo-panel-row")).to_have_count(1)
    panel.locator(".seo-note-form textarea").fill("Позвонить продавцу")
    panel.locator(".seo-note-form button[type=submit]").click()
    expect(panel).to_contain_text("Позвонить продавцу")

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(page.locator(".seo-notes")).to_have_text("💬 1")
    assert SiteNote.objects.filter(site=known).count() == 1
    assert same_document(page)


def test_decision_panel_on_sites_list(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Статус в «Площадках» — решение панелью: кнопки по порядку, запись, строка на месте."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    mark(page)
    status = page.locator("#result_list a[title='Сменить статус']")
    expect(status).to_have_text("Новая")
    panel = page.locator(PANEL)

    # Esc — панель закрылась, ничего не поменялось.
    status.click()
    expect(panel.locator(".seo-choice-item")).to_have_text([choice.label for choice in SiteStatus])
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()

    status.click()
    panel.locator(".seo-choice-item", has_text="Отбрасываю").click()
    panel.locator("textarea[name=comment]").fill("Не тематика")
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    expect(status).to_have_text("Отбрасываю")
    expect(page.locator(".seo-toast-success")).to_contain_text("known.com · Convertio — Отбрасываю")
    row = ProductSite.objects.get(site=known)
    assert (row.status, row.comment) == (SiteStatus.DISCARDED, "Не тематика")
    assert same_document(page)


# --- Размещения -------------------------------------------------------------


@pytest.fixture
def placements() -> list[Placement]:
    product = Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})[
        0
    ]
    return [
        Placement.objects.create(site=Site.objects.create(domain=domain), product=product)
        for domain in ("alpha.com", "beta.com")
    ]


def _server_today() -> str:
    return f"{timezone.localdate():%Y-%m-%d}"


def test_placement_status_from_list(
    admin_page: Page, live_server: LiveServer, placements: list[Placement]
) -> None:
    """Критерий E9-11: статус размещения из списка без перехода, строка на месте."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/?o=1")
    mark(page)
    row = page.locator("#result_list tbody tr", has_text="alpha.com")
    row.locator("a[title='Сменить статус']").click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-title")).to_have_text("alpha.com · Convertio")
    # Колонка площадок видна: панель начинается правее неё.
    site_cell = row.locator("th, td").nth(1).bounding_box()
    box = panel.bounding_box()
    assert site_cell is not None and box is not None
    assert box["x"] >= site_cell["x"] + site_cell["width"]

    # «Размещено» ставит сегодняшний день в пустую дату публикации.
    panel.locator(".seo-choice-item", has_text="Размещено").click()
    published = panel.locator("input[name=published_at]")
    expect(published).to_have_value(_server_today())
    expect(panel.locator(".seo-day.seo-day-auto")).to_have_count(1)
    panel.locator("input[name=article_url]").fill("https://alpha.com/blog/post/")
    panel.locator("[data-panel-save]").click()

    expect(panel).to_be_hidden()
    expect(row.locator("a[title='Сменить статус']")).to_have_text("Размещено")
    expect(page.locator(".seo-toast-success")).to_have_text(
        "Размещение «alpha.com · Convertio» — сохранено."
    )
    assert same_document(page)
    placement = Placement.objects.get(site__domain="alpha.com")
    assert placement.status == PlacementStatus.PLACED
    assert placement.published_at is not None
    assert f"{timezone.localtime(placement.published_at):%Y-%m-%d}" == _server_today()
    decision = ProductSite.objects.get(site=placement.site, product=placement.product)
    assert decision.status == SiteStatus.PLACED


def test_status_history_in_panel_and_card(
    admin_page: Page, live_server: LiveServer, placements: list[Placement]
) -> None:
    """Критерий E1-13: смена статуса — в истории размещения и в карточке площадки."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/?o=1")
    mark(page)
    status = page.locator("#result_list tbody tr", has_text="alpha.com").locator(
        "a[title='Сменить статус']"
    )
    panel = page.locator(PANEL)
    status.click()
    panel.locator(".seo-choice-item", has_text="Заявка отправлена").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()

    # Размещение: история сразу под кнопками статуса, новые сверху.
    status.click()
    history = panel.locator("ul.seo-status-list li")
    expect(history).to_have_count(2)
    expect(history.first).to_contain_text("В работе → Заявка отправлена")
    expect(history.first).to_contain_text("admin, панель")
    expect(history.last).to_contain_text("Создано: В работе")

    # Карточка площадки из той же панели: у Convertio две смены (словарь общий,
    # ADR-062: размещение «В работе» сразу двигает площадку), список раскрывается.
    panel.locator("a[data-panel]", has_text="Карточка площадки").click()
    expect(panel.locator(".seo-panel-title")).to_have_text("alpha.com")
    summary = panel.locator(".seo-status-history summary")
    expect(summary).to_have_text("история (2)")
    changes = panel.locator(".seo-status-history li")
    expect(changes.first).to_be_hidden()
    summary.click()
    expect(changes.first).to_contain_text("В работе → Заявка отправлена")
    expect(changes.last).to_contain_text("Новая → В работе")
    # «по размещению» — само размещение в той же панели.
    changes.first.locator("a", has_text="по размещению").click()
    expect(panel.locator(".seo-panel-title")).to_have_text("alpha.com · Convertio")
    assert same_document(page)


def test_keys_and_unsaved_changes(
    admin_page: Page, live_server: LiveServer, placements: list[Placement]
) -> None:
    """↑/↓ — соседняя запись; Esc с правками — вопрос; «Сохранить» из вопроса — дальше."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/?o=1")
    mark(page)
    panel = page.locator(PANEL)
    title = panel.locator(".seo-panel-title")
    page.locator("#result_list a", has_text="alpha.com").click()
    expect(title).to_have_text("alpha.com · Convertio")
    page.keyboard.press("ArrowDown")
    expect(title).to_have_text("beta.com · Convertio")
    expect(page.locator("#result_list tr.seo-panel-row")).to_contain_text("beta.com")
    page.keyboard.press("ArrowUp")
    expect(title).to_have_text("alpha.com · Convertio")

    # Правка и Esc — вопрос; «Остаться» — правка на месте; «Не сохранять» — закрыть.
    ask = panel.locator(".seo-panel-ask")
    panel.locator("textarea[name=comment]").fill("Статья без картинок")
    page.keyboard.press("Escape")
    expect(ask).to_contain_text("Правки не сохранены")
    ask.locator("[data-ask=stay]").click()
    expect(ask).to_have_count(0)
    expect(panel.locator("textarea[name=comment]")).to_have_value("Статья без картинок")
    page.keyboard.press("Escape")
    ask.locator("[data-ask=drop]").click()
    expect(panel).to_be_hidden()
    assert Placement.objects.get(site__domain="alpha.com").comment is None

    # Щелчок мимо панели с правками тоже спрашивает; «Сохранить» и ↓ — запись и дальше.
    page.locator("#result_list a", has_text="alpha.com").click()
    panel.locator("textarea[name=comment]").fill("Статья без картинок")
    other = page.locator("#result_list tbody tr", has_text="beta.com").locator(
        "input.action-select"
    )
    other.click()
    expect(ask).to_be_visible()
    ask.locator("[data-ask=stay]").click()
    expect(other).not_to_be_checked()
    panel.locator(".seo-panel-title").click()
    page.keyboard.press("ArrowDown")
    ask.locator("[data-ask=save]").click()
    expect(title).to_have_text("beta.com · Convertio")
    assert Placement.objects.get(site__domain="alpha.com").comment == "Статья без картинок"
    assert same_document(page)


# Списки с панелью: адрес, ссылка записи и поле, которое ломаем для ошибки формы.
LISTS = {
    "placements": ("/admin/placements/placement/", "input[name=price_paid_cents]", "abc"),
    "catalog": ("/admin/sites/site/", "input[name=domain]", ""),
    "sellers": ("/admin/sites/seller/", "input[name=name]", ""),
    "keywords": ("/admin/keywords/keyword/", "input[name=keyword]", ""),
    "site_lists": ("/admin/sites/sitelist/", "input[name=name]", ""),
    "products": ("/admin/sites/product/", "input[name=name]", ""),
    "settings": ("/admin/content/domainsetting/", "textarea[name=value]", "{"),
}


@pytest.fixture
def records(placements: list[Placement], known: Site) -> None:
    """По записи в каждом списке; настройки и продавец Collaborator — из миграций."""
    product = Product.objects.get(name="Convertio")
    Keyword.objects.create(product=product, keyword="mp4 to mp3", target_url="https://x.co/")


@pytest.mark.parametrize("name", LISTS)
def test_every_list_opens_records_in_panel(
    admin_page: Page, live_server: LiveServer, records: None, name: str
) -> None:
    """Критерий E9-11: запись — панелью, Ctrl — полной страницей; ошибка формы — в панели."""
    url, field, bad = LISTS[name]
    page = admin_page
    page.goto(f"{live_server.url}{url}")
    mark(page)
    link = page.locator("#result_list tbody tr").first.locator("th a, td a").first
    href = link.get_attribute("href")
    assert href is not None and "/change/" in href

    with page.context.expect_page() as opened:
        link.click(modifiers=["Control"])
    # Вкладка открывается пустой и потом переходит по ссылке — ждём адрес.
    full = opened.value
    full.wait_for_url(re.compile(r"/change/"))
    full.close()
    expect(page.locator(PANEL)).to_have_count(0)

    link.click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-head")).to_be_visible()
    panel.locator(field).fill(bad)
    panel.locator("[data-panel-save]").click()
    expect(panel.locator(".errorlist, .errornote").first).to_be_visible()
    expect(panel).to_be_visible()
    expect(page).to_have_url(f"{live_server.url}{url}")
    assert same_document(page)


def test_sites_list_card_and_decision_with_ctrl(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """«Площадки»: Ctrl по домену — карточка страницей, по статусу — полная форма решения."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    for selector, path in (
        ("#result_list a[title='Карточка площадки']", f"/admin/sites/site/{known.pk}/card/"),
        ("#result_list a[title='Сменить статус']", "/admin/sites/productsite/"),
    ):
        with page.context.expect_page() as opened:
            page.locator(selector).click(modifiers=["Control"])
        full = opened.value
        full.wait_for_url(re.compile(re.escape(path)))
        full.close()
    expect(page.locator(PANEL)).to_have_count(0)


def test_add_in_panel(admin_page: Page, live_server: LiveServer) -> None:
    """«Добавить продавец» — пустая форма в панели; записали — строка в списке."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/seller/")
    mark(page)
    page.locator(".object-tools a.addlink").click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-title")).to_have_text("Продавец — новая запись")
    panel.locator("input[name=name]").fill("Adsy")
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    expect(page.locator("#result_list")).to_contain_text("Adsy")
    expect(page.locator(".seo-toast-success")).to_contain_text("Продавец «Adsy» — добавлено.")
    assert Seller.objects.filter(name="Adsy").exists()
    assert same_document(page)


def test_status_in_row_at_once(admin_page: Page, live_server: LiveServer, known: Site) -> None:
    """Статус в строке меняется сразу после «Сохранить», не дожидаясь списка.

    Фоновый запрос строки (_seo_row) подменён пустым ответом — ячеек из списка не
    будет, а новый статус всё равно виден. Обрыв запроса не годится: браузер
    пишет его в консоль ошибкой.
    """
    page = admin_page
    page.route(
        re.compile(r".*_seo_row=.*"),
        lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    status = page.locator("#result_list a[title='Сменить статус']")
    status.click()
    panel = page.locator(PANEL)
    panel.locator(".seo-choice-item", has_text="Просмотрено").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    expect(status).to_have_text("Просмотрено")


def test_row_out_of_filter_stays_faded(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Фильтр «Новая», поставили «Отбрасываю» — строка остаётся, блёклая (решение 03.10),
    и статус в ней новый (просьба пользователя 04.10)."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?status__exact=new")
    mark(page)
    status = page.locator("#result_list a[title='Сменить статус']")
    status.click()
    panel = page.locator(PANEL)
    panel.locator(".seo-choice-item", has_text="Отбрасываю").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    row = page.locator("#result_list tbody tr", has_text="known.com")
    expect(row).to_have_class(re.compile(r"\bseo-row-stale\b"))
    expect(row).to_have_attribute("title", re.compile("не подходит под фильтры"))
    # Блёклая, но со свежим статусом: строка пришла мимо фильтров (_seo_row).
    expect(row.locator("a[title='Сменить статус']")).to_have_text("Отбрасываю")
    assert same_document(page)


# Ширина панели и есть ли прокрутка вбок внутри неё.
PANEL_SIZE = """() => {
  const panel = document.querySelector('.seo-panel');
  const body = panel.querySelector('.seo-panel-body');
  return {width: panel.offsetWidth, overflow: body.scrollWidth - body.clientWidth};
}"""


def test_panel_width_fits_content(
    admin_page: Page, live_server: LiveServer, placements: list[Placement], known: Site
) -> None:
    """Ширина — по содержимому, без пустого места справа (пользователь, 03.10.2026).

    Широкий экран: панель не растягивается до колонки доменов, а остаётся
    такой, чтобы поместилось содержимое; прокрутки вбок нет.
    """
    page = admin_page
    page.set_viewport_size({"width": 1920, "height": 1000})
    panel = page.locator(PANEL)

    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    page.locator("#result_list a[title='Сменить статус']").click()
    expect(panel.locator(".seo-choice")).to_be_visible()
    size = page.evaluate(PANEL_SIZE)
    assert 480 <= size["width"] <= 720, size
    assert size["overflow"] <= 0, size
    page.keyboard.press("Escape")

    page.goto(f"{live_server.url}/admin/placements/placement/")
    page.locator("#result_list a", has_text="alpha.com").click()
    expect(panel.locator(".seo-panel-title")).to_be_visible()
    size = page.evaluate(PANEL_SIZE)
    assert 480 <= size["width"] <= 800, size
    assert size["overflow"] <= 0, size
    page.keyboard.press("Escape")

    page.goto(f"{live_server.url}/admin/sites/seller/")
    page.locator("#result_list tbody a").first.click()
    expect(panel.locator(".seo-panel-head")).to_be_visible()
    size = page.evaluate(PANEL_SIZE)
    assert 480 <= size["width"] <= 720, size
    assert size["overflow"] <= 0, size
