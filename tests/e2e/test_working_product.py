"""Рабочий продукт в шапке и проверка адреса в «Размещениях» (E9-12)."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.integrations.serp import SerpPage, SerpResult
from apps.placements import indexation, tasks
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site
from apps.workspace.products import chosen_product_id

from .conftest import mark, same_document, soft_load

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PLACEMENTS = "/admin/placements/placement/"
URL = "https://conv0.com/blog/post/"


@pytest.fixture
def placements() -> tuple[Product, Product]:
    convertio = Product.objects.create(name="Convertio", domain="convertio.co")
    clideo = Product.objects.create(name="Clideo", domain="clideo.com")
    for number in range(3):
        for product in (convertio, clideo):
            Placement.objects.create(
                site=Site.objects.create(domain=f"{product.name.lower()[:4]}{number}.com"),
                product=product,
                status=PlacementStatus.PLACED,
                article_url=f"https://{product.name.lower()[:4]}{number}.com/blog/post/",
            )
    return convertio, clideo


def _sites(page: Page) -> set[str]:
    texts = page.locator("#result_list tbody .field-site_link > a").all_inner_texts()
    return {text.strip() for text in texts if text.strip()}


def test_switch_product_in_header(
    admin_page: Page,
    live_server: LiveServer,
    admin_user: User,
    placements: tuple[Product, Product],
) -> None:
    """Выбор в шапке: список сразу под новый продукт, без перезагрузки; помнится в базе."""
    page = admin_page
    convertio, clideo = placements
    page.goto(f"{live_server.url}{PLACEMENTS}")
    mark(page)
    switch = page.locator(".seo-product-switch select")
    expect(switch).to_have_value(str(convertio.pk))
    expect(page.locator("#result_list tbody tr")).to_have_count(3)
    assert all(name.startswith("conv") for name in _sites(page))

    # Фильтр в колонке — лупа: сужает по «другим продуктам», шапку не меняет.
    dropdown = page.locator(".list-filter-dropdown").filter(has_text="Продукт")
    with soft_load(page):
        dropdown.locator("select").select_option(label="Clideo")
    expect(page.locator("#result_list tbody tr")).to_have_count(0)
    expect(switch).to_have_value(str(convertio.pk))

    with soft_load(page):
        dropdown.locator("select").select_option(label="Все")
    expect(page.locator("#result_list tbody tr")).to_have_count(3)

    # Шапка — Clideo: список на месте, выбор из колонки уходит.
    page.evaluate("window.__held = document.querySelector('#changelist-filter'); 0")
    with soft_load(page):
        switch.select_option(str(clideo.pk))
    expect(page.locator("#result_list tbody tr")).to_have_count(3)
    assert all(name.startswith("clid") for name in _sites(page))
    assert "product" not in page.url
    assert page.evaluate("window.__held === document.querySelector('#changelist-filter')")
    assert chosen_product_id(admin_user) == clideo.pk
    assert same_document(page)

    # После перезагрузки и на главной — тот же продукт.
    page.reload()
    expect(page.locator(".seo-product-switch select")).to_have_value(str(clideo.pk))
    page.goto(f"{live_server.url}/admin/")
    expect(page.locator(".seo-product-switch select")).to_have_value(str(clideo.pk))


class FakeSearch:
    def __init__(self, found: list[str]) -> None:
        self.found = found

    def __call__(self, query: str, **kwargs: Any) -> SerpPage:
        results = tuple(SerpResult(i, u, "x", "", "") for i, u in enumerate(self.found, 1))
        return SerpPage(query, 10, "us", results, None, timezone.now())


def test_check_url_shows_verdict(
    admin_page: Page,
    live_server: LiveServer,
    placements: tuple[Product, Product],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Адрес — «Проверить»: зелёное окошко вверху; не найден — красное; введённое не стирается."""
    page = admin_page
    monkeypatch.setattr(indexation, "search", FakeSearch([URL]))
    monkeypatch.setattr(tasks.check_url_indexation, "throttle", None)
    page.goto(f"{live_server.url}{PLACEMENTS}")
    field = page.locator('input[form="seo-url-check"]')
    field.fill("conv0.com/blog/post/")
    field.press("Enter")
    verdict = page.locator(".seo-verdict.is-yes")
    expect(verdict).to_contain_text("В индексе")
    expect(verdict).to_contain_text(URL)
    box = page.locator(".seo-verdicts").bounding_box()
    assert box is not None and box["y"] < 40  # вверху экрана
    # Ни строки журнала, ни отметки у размещения.
    placement = Placement.objects.get(article_url=URL)
    assert placement.is_indexed is None

    monkeypatch.setattr(indexation, "search", FakeSearch([]))
    page.locator('button[form="seo-url-check"]').click()
    expect(page.locator(".seo-verdict.is-no")).to_contain_text("Не в индексе")

    # Фильтр не стирает адрес в поле.
    with soft_load(page):
        page.locator("#result_list thead th.column-site_link .text a").dispatch_event("click")
    expect(field).to_have_value("conv0.com/blog/post/")

    field.fill("не адрес")
    field.press("Enter")
    expect(page.locator(".seo-verdict.is-error")).to_contain_text("не адрес страницы")
