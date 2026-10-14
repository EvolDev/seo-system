"""«Экономика» в браузере: подсказки графиков, отбор размещений плиткой и облачками,
поле суммы у действия «Цена написания…» в «Площадках» (ADR-071)."""

import datetime as dt

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

ECONOMICS = "/admin/economics/"
SITES = "/admin/sites/productsitelatest/"


@pytest.fixture
def product() -> Product:
    convertio = Product.objects.create(name="Convertio", domain="convertio.co")
    now = timezone.now()
    for number in range(3):
        Placement.objects.create(
            site=Site.objects.create(domain=f"placed{number}.com"),
            product=convertio,
            status=PlacementStatus.PLACED,
            published_at=now - dt.timedelta(hours=number),
            price_paid_cents=20000,
        )
    Placement.objects.create(
        site=Site.objects.create(domain="ordered.com"),
        product=convertio,
        status=PlacementStatus.ORDERED,
        ordered_at=now,
        price_paid_cents=15000,
    )
    return convertio


def test_tooltip_and_rows(admin_page: Page, live_server: LiveServer, product: Product) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{ECONOMICS}?product={product.pk}")
    expect(page.locator(".seo-econ-tile-spent .seo-econ-tile-value")).to_have_text("€600")
    expect(page.locator(".seo-econ-tile-planned .seo-econ-tile-value")).to_have_text("€150")
    # Подсказка месяца — у курсора, текстом из данных графика.
    money, links = page.locator(".seo-econ-chart").all()
    money.locator(".seo-econ-hit").last.hover()
    tip = page.locator(".seo-econ-tip")
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("Потрачено: €600")
    links.locator(".seo-econ-hit").last.hover()
    expect(tip).to_contain_text("Размещено: 3 ссылки")
    # Подписи графика — 12 px на экране, как бы ни растянулся рисунок.
    size = page.locator(".seo-econ-svg .seo-econ-tick").first.evaluate(
        "node => node.getBoundingClientRect().height"
    )
    assert 10 <= size <= 18
    rows = page.locator("[data-econ-rows] tbody tr[data-kind]")
    expect(rows).to_have_count(4)
    # Плитка «Запланировано» — кнопка: внизу остаются только размещения плана.
    page.locator(".seo-econ-tile-planned").click()
    expect(page.locator("[data-econ-rows] tbody tr[data-kind]:visible")).to_have_count(1)
    expect(page.locator('[data-econ-filter="planned"]')).to_have_attribute("aria-pressed", "true")
    page.locator('[data-econ-filter="all"]').click()
    expect(page.locator("[data-econ-rows] tbody tr[data-kind]:visible")).to_have_count(4)


def test_period_without_reload(admin_page: Page, live_server: LiveServer, product: Product) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{ECONOMICS}?product={product.pk}")
    page.evaluate("window.__seoSameDocument = true")
    page.locator(".seo-econ-segments a", has_text="Год").click()
    expect(page.locator(".seo-econ-period")).to_have_text(f"{timezone.localdate().year} год")
    assert page.evaluate("window.__seoSameDocument === true")
    # Подсказки работают и после подгрузки экрана.
    page.locator(".seo-econ-hit").first.hover()
    expect(page.locator(".seo-econ-tip")).to_be_visible()


def test_writing_field_follows_action(
    admin_page: Page, live_server: LiveServer, product: Product
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}{SITES}")
    field = page.locator("input[name=writing]")
    expect(field).to_be_hidden()
    page.locator("select[name=action]").select_option("set_writing_action")
    expect(field).to_be_visible()
    page.locator("select[name=action]").select_option("set_status_action")
    expect(field).to_be_hidden()
