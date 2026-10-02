"""Наши экраны на общей подгрузке (E9-09, ADR-046): загрузки, карточка площадки."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django import Settings
from pytest_django.live_server_helper import LiveServer

from apps.integrations.serp import SerpPage, SerpResult
from apps.placements import indexation, tasks
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    ExchangeRate,
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

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PRICE_DATE = dt.date(2026, 10, 1)
CSV = b"Website,GP Price,DA\nknown.com,$150,30\nnew.com,$90,20\n"


@pytest.fixture(autouse=True)
def uploads_dir(settings: Settings, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"


@pytest.fixture
def known() -> Site:
    """known.com с рабочей ценой Collaborator €200 и продавец LinkHub в долларах."""
    ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.1355"))
    Seller.objects.create(name="LinkHub Media", currency="USD")
    Product.objects.create(name="Convertio", domain="convertio.co")
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
    return site


def test_price_list_upload_to_review(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Файл → колонки → сводка → запись → разбор: всё без перезагрузки."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)

    with soft_load(page):
        page.locator("a.home-card", has_text="Загрузки").click()
    with soft_load(page):
        page.locator(".object-tools a", has_text="Загрузить файл").click()

    # Шаг 1: файл уходит запросом с ходом отправки, ответ показывает общая подгрузка.
    page.locator("input[name=kind][value=price_list]").check()
    page.locator("select[name=seller]").select_option(label="LinkHub Media")
    page.locator("input[name=prices_date]").fill(PRICE_DATE.isoformat())
    page.locator("input[name=file]").set_input_files(
        {"name": "linkhub.csv", "mimeType": "text/csv", "buffer": CSV}
    )
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Дальше").click()
    expect(page).to_have_url(f"{live_server.url}/admin/sites/upload/1/columns/")
    expect(page.locator("#content")).to_contain_text("GP Price")

    # Шаг 2: колонки — обычная форма, её отправляет общая подгрузка.
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Дальше: сводка").click()
    expect(page).to_have_url(f"{live_server.url}/admin/sites/upload/1/summary/")

    # Шаг 3: запись — запрос с ожиданием задачи, по готовности — разбор.
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Записать в базу").click()
    expect(page).to_have_url(f"{live_server.url}/admin/sites/upload/1/review/")
    expect(page.locator("#content")).to_contain_text("known.com")

    # «Назад» — снова сводка, без перезагрузки.
    with soft_load(page):
        page.evaluate("history.back()")
    expect(page).to_have_url(f"{live_server.url}/admin/sites/upload/1/summary/")
    assert same_document(page)


def test_site_card_changes_refresh_list_in_place(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Заметка в карточке → закрыть → «Площадки» обновились на месте."""
    site_list = SiteList.objects.create(name="Октябрь")
    SiteListItem.objects.create(site_list=site_list, site=known)
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    mark(page)
    expect(page.locator(".seo-notes")).to_have_count(0)

    page.locator("#result_list a[data-site-card]", has_text="known.com").click()
    page.locator(".seo-note-form textarea").fill("Позвонить продавцу")
    page.locator(".seo-note-form button[type=submit]").click()
    expect(page.locator(".seo-card")).to_contain_text("Позвонить продавцу")

    with soft_load(page):
        page.keyboard.press("Escape")
    expect(page.locator(".seo-notes")).to_have_text("💬 1")
    assert SiteNote.objects.filter(site=known).count() == 1
    assert same_document(page)


class _FoundEverywhere:
    """Выдача Google без Serper: статья всегда в индексе. Платных запросов нет."""

    def __init__(self, url: str) -> None:
        self.url = url

    def __call__(self, query: str, **kwargs: object) -> SerpPage:
        result = SerpResult(1, self.url, "example.com", "", "")
        return SerpPage(query, 10, "us", (result,), None, timezone.now())


def test_indexation_buttons_after_soft_navigation(
    admin_page: Page, live_server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка ↻, действие для отмеченных и кнопка в форме — после переходов."""
    article = "https://example.com/blog/post/"
    monkeypatch.setattr(indexation, "search", _FoundEverywhere(article))
    monkeypatch.setattr(tasks.check_indexation, "throttle", None)
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="example.com")
    Placement.objects.create(
        site=site, product=product, status=PlacementStatus.PUBLISHED, article_url=article
    )
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)
    with soft_load(page):
        page.locator("a.home-card", has_text="Размещения").click()

    # Итог — зелёное окно; гаснет само через 8 с, поэтому после проверки убираем.
    done = page.locator(".seo-toast.is-success")
    clear = "document.querySelectorAll('.seo-toast').forEach(toast => toast.remove())"

    page.locator(".seo-index-check").first.click()
    expect(done).to_have_count(1)
    page.evaluate(clear)

    page.locator("input.action-select").first.check()
    page.locator("select[name=action]").select_option("check_indexation_action")
    page.locator("#changelist-form button[name=index]").click()
    expect(done).to_have_count(1)
    page.evaluate(clear)

    with soft_load(page):
        page.locator("#result_list a", has_text="example.com").click()
    page.locator("form[data-indexation-form] button").click()
    expect(done).to_have_count(1)
    assert same_document(page)


def test_status_window_on_sites_list(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Статус в «Площадках» — окно «Решение по площадке» поверх списка (E9-09)."""
    site_list = SiteList.objects.create(name="Октябрь")
    SiteListItem.objects.create(site_list=site_list, site=known)
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/")
    mark(page)
    status = page.locator("#result_list a[data-decision]")
    expect(status).to_have_text("Новая")

    # Esc — окно закрылось, ничего не поменялось.
    status.click()
    window = page.locator("dialog.seo-dialog")
    expect(window.locator("select[name=status]")).to_be_visible()
    page.keyboard.press("Escape")
    expect(window).not_to_be_visible()

    # Выбрали статус, сохранили — окно закрылось, строка показывает новый статус.
    # Статусы в окне — в согласованном порядке (E1-12).
    status.click()
    options = window.locator("select[name=status] option")
    expect(options).to_have_text([choice.label for choice in SiteStatus])
    window.locator("select[name=status]").select_option(label="Отбрасываю")
    with soft_load(page):
        window.locator("button[type=submit]").click()
    expect(window).not_to_be_visible()
    expect(page.locator("#result_list a[data-decision]")).to_have_text("Отбрасываю")
    expect(page.locator(".seo-toast-success")).to_contain_text("known.com · Convertio — Отбрасываю")
    assert ProductSite.objects.get(site=known).status == SiteStatus.DISCARDED
    assert same_document(page)
