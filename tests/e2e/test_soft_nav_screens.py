"""Наши экраны на общей подгрузке (E9-09, ADR-046): загрузки, проверка индексации.

Карточка площадки и решение по площадке — панелью, tests/e2e/test_panel.py.
"""

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
    Seller,
    Site,
    SiteMetric,
    SitePrice,
)
from apps.sites.uploads.plan import start_of_day

from .conftest import mark, pick, same_document, soft_load

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
    pick(page, "seller", "LinkHub Media")
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
        site=site, product=product, status=PlacementStatus.PLACED, article_url=article
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

    # Размещение открывается панелью (E9-11): кнопка проверки — в группе «Проверки».
    page.locator("#result_list a", has_text="example.com").click()
    page.locator(".seo-panel button[data-indexation-button]").click()
    expect(done).to_have_count(1)
    expect(page.locator(".seo-panel .field-is_indexed .readonly img")).to_have_attribute(
        "alt", "True"
    )
    assert same_document(page)


PLACEMENTS_CSV = (
    "Target;Person;Source;URL статьи;Статус;Итог цена\n"
    "known.com;Evgeniy;Athena Smith;https://known.com/post;Размещено;311,81\n"
    "fresh.com;Lilith;Athena Smith;https://fresh.com/x;;120\n"
).encode()


def test_placements_upload_form_and_path(
    admin_page: Page, live_server: LiveServer, known: Site
) -> None:
    """Файл размещений (E1-09): поля по типу файла, колонки, сводка, запись — без перезагрузки."""
    Product.objects.create(name="Clideo", domain="clideo.com")
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/")
    mark(page)
    product_row = page.locator(".form-row", has=page.locator("select[name=product]"))
    employee_row = page.locator(".form-row", has=page.locator("select[name=employee]"))
    country_row = page.locator(".form-row", has=page.locator("[data-country-picker]"))
    date_label = page.locator("label[for=id_prices_date]")

    # Прайс — продавец, без продукта и сотрудника.
    page.locator("input[name=kind][value=price_list]").check()
    expect(product_row).to_be_hidden()
    expect(employee_row).to_be_hidden()
    expect(page.locator(".seo-picker:has(select[name=seller])")).to_be_visible()
    # Ссылающиеся домены — только продукт, дата — выгрузки.
    page.locator("input[name=kind][value=ahrefs_refdomains]").check()
    expect(product_row).to_be_visible()
    expect(page.locator(".seo-picker:has(select[name=seller])")).to_be_hidden()
    expect(country_row).to_be_hidden()
    expect(date_label).to_have_text("Дата выгрузки")
    # Размещения — продукт, продавец и сотрудник, дата — файла.
    page.locator("input[name=kind][value=placements]").check()
    expect(product_row).to_be_visible()
    expect(employee_row).to_be_visible()
    expect(date_label).to_have_text("Дата файла")

    page.locator("select[name=product]").select_option(label="Clideo")
    page.locator("input[name=file]").set_input_files(
        {"name": "clideo.csv", "mimeType": "text/csv", "buffer": PLACEMENTS_CSV}
    )
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Дальше").click()
    expect(page.locator("#content")).to_contain_text("Разметка запомнится для файлов размещений")
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Дальше: сводка").click()
    expect(page.locator("#content")).to_contain_text("размещений будет создано")
    with soft_load(page):
        page.locator("button[type=submit]", has_text="Записать в базу").click()
    expect(page.locator("#content")).to_contain_text("размещений создано")
    assert same_document(page)
    assert Placement.objects.filter(product__name="Clideo").count() == 2

    # Форма снова — сразу с размещениями, как в прошлый раз; продукт — рабочий
    # из шапки (ADR-057), а не прошлый.
    page.goto(f"{live_server.url}/admin/sites/upload/add/")
    expect(page.locator("input[name=kind][value=placements]")).to_be_checked()
    expect(page.locator("select[name=product] option:checked")).to_have_text("Convertio")
    expect(employee_row).to_be_visible()
