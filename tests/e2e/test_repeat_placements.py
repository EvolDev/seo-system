"""Повторные размещения в браузере (ADR-073): вкладки площадки в панели.

«+ Ещё размещение» через выбранного продавца — новое размещение сразу вкладкой
в той же панели; красная корзина на вкладке удаляет размещение с
подтверждением в панели; чужой продукт — только просмотр; «Взять в
размещение» для отмеченных — окно с «ещё одно» галочкой. Списки: статус —
фишками размещений, быстрые фильтры и «Период ▾» без перезагрузки, одна строка
на площадку в «Размещениях»; «Больше не работаем» — красной фишкой. Серверную
часть проверяют tests/test_repeat_placements.py и tests/test_status_chips.py.
"""

import re

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement, PlacementStatus
from apps.sites import latest
from apps.sites.models import Product, Seller, Site, SitePrice

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})[0]


@pytest.fixture
def space(convertio: Product) -> Site:
    """spacecoastdaily.com: рабочая цена Collaborator и LinkHouse дешевле, №1 — заявка."""
    site = Site.objects.create(domain="spacecoastdaily.com")
    working = SitePrice.objects.create(
        site=site, seller=Seller.collaborator(), placement_cents=12650, reviewed_at=timezone.now()
    )
    SitePrice.objects.create(
        site=site,
        seller=Seller.objects.create(name="LinkHouse"),
        placement_cents=9500,
        reviewed_at=timezone.now(),
    )
    site.price = working
    site.save(update_fields=["price"])
    latest.refresh([site.pk])
    Placement.objects.create(
        site=site,
        product=convertio,
        status=PlacementStatus.ORDERED,
        seller=working.seller,
        price=working,
    )
    return site


def test_second_placement_through_tabs(
    admin_page: Page, live_server: LiveServer, space: Site
) -> None:
    """№1 — заявка; «+ Ещё размещение» через LinkHouse → №2 вкладкой в той же панели."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    mark(page)
    page.locator("#result_list a", has_text="spacecoastdaily.com").first.click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-ptab-on")).to_contain_text("№1 · Collaborator")
    panel.locator(".seo-ptab-add").click()
    expect(panel.locator(".seo-panel-title")).to_have_text("spacecoastdaily.com — новое размещение")
    panel.locator("label", has_text="LinkHouse").click()
    panel.locator("[data-panel-save]").click()
    expect(panel.locator(".seo-ptab-on")).to_contain_text("№2 · LinkHouse")
    expect(panel.locator(".seo-ptab-deal")).to_have_count(2)
    second = Placement.objects.filter(site=space).order_by("pk").last()
    assert second is not None and second.price is not None
    assert second.price.seller.name == "LinkHouse"
    # Закрыли — список перечитан: строка одна, в статусе — оба размещения.
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    row = page.locator("#result_list tbody tr", has_text="spacecoastdaily.com")
    expect(row).to_have_count(1)
    expect(row.locator(".seo-st")).to_have_count(2)
    expect(row.locator(".seo-st").nth(1)).to_contain_text("В работе")
    assert same_document(page)


def test_trash_on_tab_deletes(admin_page: Page, live_server: LiveServer, space: Site) -> None:
    """Корзина на вкладке №2 → подтверждение в панели → удалено, список перечитан."""
    product = Product.objects.get(name="Convertio")
    extra = Placement.objects.create(site=space, product=product)
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    mark(page)
    page.locator(f"#result_list a[href$='/{extra.pk}/change/']").first.click()
    panel = page.locator(PANEL)
    panel.locator(".seo-ptab-on .seo-ptab-trash").click()
    expect(panel).to_contain_text("Удалить")
    panel.locator("button[type=submit]", has_text="Удалить").click()
    expect(panel).to_be_hidden()
    expect(page.locator("#result_list tbody tr", has_text="spacecoastdaily.com")).to_have_count(1)
    assert not Placement.objects.filter(pk=extra.pk).exists()
    assert same_document(page)


def test_other_product_is_read_only(admin_page: Page, live_server: LiveServer, space: Site) -> None:
    """В карточке площадки Clideo — только просмотр: карточка его размещения без записи."""
    clideo = Product.objects.create(name="Clideo", domain="clideo.com")
    theirs = Placement.objects.create(
        site=space,
        product=clideo,
        status=PlacementStatus.PLACED,
        article_url="https://spacecoastdaily.com/clideo-review/",
    )
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
    mark(page)
    # В «Площадках» домен открывает карточку площадки — вкладку «Площадка».
    page.locator("#result_list a[data-panel]", has_text="spacecoastdaily.com").first.click()
    panel = page.locator(PANEL)
    expect(panel.locator(".seo-ptab-on")).to_contain_text("Площадка")
    other = panel.locator(".seo-card-other", has_text="Clideo")
    expect(other).to_contain_text("только просмотр")
    expect(other.locator("a", has_text="сменить")).to_have_count(0)
    other.locator("a", has_text="карточка размещения").click()
    expect(panel.locator("[data-panel-save]")).to_have_count(0)
    expect(panel.locator(".seo-ptab-trash")).to_have_count(0)
    theirs.refresh_from_db()
    assert theirs.status == PlacementStatus.PLACED


def test_take_action_asks_about_another_one(
    admin_page: Page, live_server: LiveServer, space: Site
) -> None:
    """«Взять в размещение» по отмеченным: окно, «ещё одно» — галочкой."""
    fresh = Site.objects.create(domain="fresh.example")
    latest.refresh([fresh.pk])
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
    page.locator("#action-toggle").check()
    page.locator("select[name=action]").select_option("take_placement_action")
    page.locator("button[name=index]").first.click()
    expect(page.locator(".seo-take")).to_contain_text("Заведём размещение — 1")
    expect(page.locator(".seo-take")).to_contain_text("Размещения уже есть — 1")
    page.locator(".seo-take label", has_text="spacecoastdaily.com").locator("input").check()
    page.locator(".seo-take button[type=submit]").click()
    expect(page.locator(".messagelist, .seo-toast-success").first).to_contain_text(
        "Взято в размещение: 1 · ещё одно: 1"
    )
    assert Placement.objects.filter(site=space).count() == 2
    assert Placement.objects.filter(site=fresh).count() == 1


def test_status_bar_and_period(admin_page: Page, live_server: LiveServer, space: Site) -> None:
    """Статус кнопкой над списком и «Период ▾» — без перезагрузки; меню закрывается щелчком мимо."""
    Site.objects.create(domain="fresh.example")
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
    mark(page)
    rows = page.locator("#result_list tbody tr")
    expect(rows).to_have_count(2)
    # Фишка размещения в колонке статуса, у новой площадки — статус с датой.
    expect(rows.filter(has_text="spacecoastdaily.com").locator(".seo-st")).to_contain_text(
        "Заявка отправлена"
    )
    expect(rows.filter(has_text="fresh.example").locator(".seo-decided")).to_have_count(1)
    bar = page.locator(".seo-stbar")
    with soft_load(page):
        bar.locator("a", has_text="Заявка отправлена").click()
    expect(rows).to_have_count(1)
    expect(bar.locator(".seo-stbar-chip.seo-stbar-on")).to_have_text("Заявка отправлена")
    period = bar.locator("details.seo-stbar-period")
    period.locator("summary").click()
    expect(period.locator(".seo-stbar-menu")).to_be_visible()
    page.locator("#result_list").click(position={"x": 5, "y": 5})
    expect(period.locator(".seo-stbar-menu")).to_be_hidden()
    period.locator("summary").click()
    with soft_load(page):
        period.get_by_role("link", name="Месяц", exact=True).click()
    expect(period.locator("summary")).to_contain_text("Период: месяц")
    # Выбрали — меню закрылось, хотя список обновился на месте.
    expect(period.locator(".seo-stbar-menu")).to_be_hidden()
    expect(rows).to_have_count(1)
    # Свой период — с месяца по месяц: прошлый год, размещение сегодняшнее — пусто.
    period.locator("summary").click()
    last = timezone.localdate().year - 1
    period.locator("input[name=period_from]").fill(f"{last}-01")
    period.locator("input[name=period_to]").fill(f"{last}-12")
    with soft_load(page):
        period.locator("button[type=submit]").click()
    expect(period.locator("summary")).to_contain_text(f"01.{last} — 12.{last}")
    expect(rows.filter(has_text="spacecoastdaily.com")).to_have_count(0)
    assert same_document(page)


def test_stop_decision_with_placements(
    admin_page: Page, live_server: LiveServer, space: Site
) -> None:
    """Площадка с размещением: «Больше не работаем…» → «Отказ» — красная фишка после размещения."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
    mark(page)
    page.locator("#result_list a[data-panel]", has_text="spacecoastdaily.com").first.click()
    panel = page.locator(PANEL)
    decision = panel.locator(".seo-card-decision")
    expect(decision.locator(".seo-st")).to_contain_text("Заявка отправлена")
    decision.locator("a", has_text="Больше не работаем…").click()
    expect(panel.locator(".seo-choice")).to_contain_text("Работаем — по размещениям")
    expect(panel.locator(".seo-choice")).not_to_contain_text("Просмотрено")
    panel.locator(".seo-choice label", has_text="Отказ").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    row = page.locator("#result_list tbody tr", has_text="spacecoastdaily.com")
    expect(row.locator(".seo-st-no")).to_contain_text("Отказ")
    expect(row.locator(".seo-st").first).to_contain_text("Заявка отправлена")
    # Передумали: красная фишка открывает решение, «Работаем» — фишка уходит,
    # строка обновляется, хотя ссылки, которой открывали, в ней больше нет.
    row.locator(".seo-st-no").click()
    panel.locator(".seo-choice label", has_text="Работаем — по размещениям").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    expect(row.locator(".seo-st-no")).to_have_count(0)
    expect(row.locator(".seo-st")).to_have_count(1)
    assert same_document(page)


def test_new_placement_marks_row_in_sites_list(
    admin_page: Page, live_server: LiveServer, convertio: Product
) -> None:
    """«Площадки» с фильтром «Новая»: завели размещение из панели — строка сразу
    «В работе» и бледнеет, как после решения; закрыли панель — строка на месте
    (просьба 20.10.2026: раньше строка не менялась, а после закрытия пропадала)."""
    site = Site.objects.create(domain="educba.com")
    working = SitePrice.objects.create(
        site=site, seller=Seller.collaborator(), placement_cents=17803, reviewed_at=timezone.now()
    )
    site.price = working
    site.save(update_fields=["price"])
    latest.refresh([site.pk])
    Site.objects.create(domain="fresh.example")
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/productsitelatest/?list=all")
    mark(page)
    with soft_load(page):
        page.locator(".seo-stbar a", has_text="Новая").click()
    row = page.locator("#result_list tbody tr", has_text="educba.com")
    expect(row).to_have_count(1)

    row.locator("a[data-panel]", has_text="educba.com").click()
    panel = page.locator(PANEL)
    panel.locator(".seo-ptab-add").click()
    expect(panel.locator(".seo-panel-title")).to_have_text("educba.com — новое размещение")
    panel.locator("[data-panel-save]").click()
    expect(panel.locator(".seo-ptab-on")).to_contain_text("№1 · Collaborator")

    expect(row).to_have_class(re.compile(r"\bseo-row-stale\b"))
    expect(row.locator(".seo-st")).to_contain_text("В работе")
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(row).to_have_count(1)
    expect(row).to_have_class(re.compile(r"\bseo-row-stale\b"))
    assert same_document(page)
