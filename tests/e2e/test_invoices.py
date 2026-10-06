"""Счета и статистика на главной в браузере (E1-14, ADR-055).

«Счёт на отмеченные» в «Размещениях» открывает форму счёта панелью поверх
списка, без перехода; «Поделить поровну» очищает доли; записали — колонка
«заплачено» в списке с суммами. На главной продукт и год меняются без
перезагрузки, у столбика — подсказка. Снимки главной во всех расцветках —
в test-results/e2e/home-<расцветка>.png, чтобы посмотреть на графики глазами.
"""

import datetime as dt
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.invoices import sync_paid
from apps.placements.models import Invoice, InvoiceItem, Placement, PlacementStatus
from apps.sites.models import Product, Seller, Site
from apps.sites.uploads.plan import start_of_day

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"
SHOTS = Path("test-results/e2e")


@pytest.fixture
def placements() -> list[Placement]:
    convertio, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    clideo, _ = Product.objects.get_or_create(name="Clideo", defaults={"domain": "clideo.com"})
    seller = Seller.objects.create(name="StarMedia")
    rows = []
    # Последнее — в прошлом году: на главной есть куда листать год назад.
    for day, domain, product, paid in (
        (dt.date(2026, 9, 11), "a.com", convertio, None),
        (dt.date(2026, 9, 14), "b.com", convertio, None),
        (dt.date(2026, 9, 30), "c.com", clideo, 58182),
        (dt.date(2026, 8, 20), "e.com", convertio, 33646),
        (dt.date(2025, 12, 20), "d.com", clideo, None),
    ):
        rows.append(
            Placement.objects.create(
                site=Site.objects.create(domain=domain),
                product=product,
                seller=seller,
                status=PlacementStatus.PLACED,
                published_at=start_of_day(day),
                price_paid_cents=paid,
            )
        )
    return rows


def test_invoice_for_selected_in_panel(
    admin_page: Page, live_server: LiveServer, placements: list[Placement]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/?month=2026-09&o=1")
    mark(page)
    boxes = page.locator("#result_list input.action-select")
    boxes.nth(0).check()
    boxes.nth(1).check()
    page.locator("select[name=action]").select_option("invoice_for_selected_action")
    page.locator("button[name=index]").first.click()

    panel = page.locator(PANEL)
    expect(panel.locator(".seo-panel-title")).to_contain_text("Счёт — новая запись")
    expect(panel.locator("select[name=items-0-placement] option[selected]")).to_have_count(1)
    expect(panel.locator("select[name=items-1-placement] option[selected]")).to_have_count(1)
    assert same_document(page), "форма счёта открылась переходом, а не панелью"

    panel.locator("input[name=amount_cents]").fill("100")
    panel.locator("input[name=items-0-amount_cents]").fill("70")
    panel.locator("[data-split-evenly]").click()
    expect(panel.locator("input[name=items-0-amount_cents]")).to_have_value("")
    panel.locator("input[name=pay_url]").fill("https://www.paypal.com/invoice/p/#E2E")
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()

    invoice = Invoice.objects.get()
    assert sorted(invoice.items.values_list("amount_cents", flat=True)) == [5000, 5000]
    expect(page.locator("#result_list")).to_contain_text("счёт не оплачен")
    assert same_document(page)


def test_home_stats_switch_without_reload(
    admin_page: Page, live_server: LiveServer, placements: list[Placement]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    mark(page)
    expect(page.locator(".seo-tile")).to_have_count(3)
    expect(page.locator(".seo-chart")).to_have_count(2)

    with soft_load(page):
        page.locator(".seo-stats-products a", has_text="Clideo").click()
    expect(page.locator(".seo-stats-products [aria-current]")).to_have_text("Clideo")
    assert same_document(page)

    with soft_load(page):
        page.locator(".seo-stats-year a[aria-label='Прошлый год']").click()
    expect(page.locator(".seo-stats-year b")).to_have_text("2025")
    assert same_document(page)


@pytest.mark.parametrize("palette", ["apple-light", "google-dark", "emerald", "ahrefs"])
def test_home_charts_look(
    admin_page: Page, live_server: LiveServer, placements: list[Placement], palette: str
) -> None:
    """Снимок главной в расцветке; подсказка столбика сентября видна при наведении."""
    # Сентябрь: a.com по счёту, c.com без счёта — в столбике оба цвета.
    seller = Seller.objects.get(name="StarMedia")
    invoice = Invoice.objects.create(seller=seller, amount_cents=18100)
    InvoiceItem.objects.create(invoice=invoice, placement=placements[0], amount_cents=18100)
    sync_paid({placements[0].pk})
    page = admin_page
    page.goto(f"{live_server.url}/admin/")
    page.evaluate(f"localStorage.setItem('admin-palette', '{palette}')")
    page.goto(f"{live_server.url}/admin/?year=2026")
    september = page.locator(".seo-chart-placed .seo-col").nth(8)
    september.hover()
    expect(september.locator(".seo-col-tip")).to_be_visible()
    expect(september.locator(".seo-col-tip")).to_contain_text("Сентябрь 2026")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.locator(".seo-stats").screenshot(path=SHOTS / f"home-{palette}.png")
