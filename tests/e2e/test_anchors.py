"""Анкоры продукта (E3-05, ADR-059) — seo/anchors.js.

Поле анкора с поиском подставляет адрес и не трогает свой; новый анкор —
окном, без ухода со страницы; рекомендация ставится в свободную ссылку, нет
свободной — в новую; доля на странице «Анкоры» правится на месте.
Серверную часть проверяют tests/test_anchor_screens.py и test_placements_admin.py.
"""

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.keywords.models import AnchorType, Keyword, PageTypeShare
from apps.placements.models import Placement, PlacementLink
from apps.sites.models import Product, Site
from apps.workspace.products import choose_product

from .conftest import mark, same_document

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

D = Decimal
PANEL = ".seo-panel"


@pytest.fixture
def convertio(admin_user: User) -> Product:
    product, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    choose_product(admin_user, product)
    PageTypeShare.objects.create(product=product, page_type="Video", target_pct=D(60), position=1)
    PageTypeShare.objects.create(product=product, page_type="Главная", target_pct=D(40), position=2)
    for text, url, page_type, volume in (
        ("convert", "https://convertio.co/", "Главная", 42000),
        ("mp4 converter", "https://convertio.co/mp4-converter/", "Video", 9000),
        ("mp4 to mp3", "https://convertio.co/mp4-mp3/", "Video", 5000),
        ("mov to mp4", "https://convertio.co/mov-mp4/", "Video", 4000),
    ):
        Keyword.objects.create(
            product=product,
            keyword=text,
            target_url=url,
            page_type=page_type,
            volume=volume,
            anchor_type=AnchorType.EXACT,
        )
    return product


@pytest.fixture
def placement(convertio: Product) -> Placement:
    return Placement.objects.create(site=Site.objects.create(domain="blog.com"), product=convertio)


def _row(page: Page, index: int):  # type: ignore[no-untyped-def]
    return page.locator(f"#links-{index}")


def test_pick_anchor_fills_url(
    admin_page: Page, live_server: LiveServer, convertio: Product
) -> None:
    """Критерий: анкор одним полем, адрес подтягивается, свой адрес не затирается."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/add/")
    picker = _row(page, 0).locator(".seo-picker-input")
    url = _row(page, 0).locator("input[data-anchor-url]")
    expect(_row(page, 0).locator("select[data-anchor-select]")).to_be_hidden()

    picker.fill("mp4 to")
    options = _row(page, 0).locator(".seo-picker-list li[data-index]")
    expect(options.first).to_contain_text("mp4 to mp3")
    expect(options.first).to_contain_text("Video")
    picker.press("Enter")
    expect(picker).to_have_value("mp4 to mp3")
    expect(url).to_have_value("https://convertio.co/mp4-mp3/")

    # Свой адрес — анкор сменили, а адрес человека остался.
    url.fill("https://convertio.co/mp4-to-mp3-custom/")
    picker.fill("mov")
    picker.press("Enter")
    expect(picker).to_have_value("mov to mp4")
    expect(url).to_have_value("https://convertio.co/mp4-to-mp3-custom/")
    # Enter в поле анкора не отправил форму размещения.
    assert page.url.endswith("/add/")


def test_new_anchor_in_dialog(
    admin_page: Page, live_server: LiveServer, convertio: Product
) -> None:
    """Критерий: новый анкор окном «анкор и ссылка», без ухода со страницы."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/add/")
    mark(page)
    picker = _row(page, 0).locator(".seo-picker-input")
    picker.fill("webm to gif")
    add = _row(page, 0).locator(".seo-picker-add")
    expect(add).to_have_text("＋ Новый анкор «webm to gif»…")
    add.click()
    dialog = page.locator("[data-anchor-dialog]")
    expect(dialog).to_be_visible()
    expect(dialog.locator('[data-dialog-field="keyword"]')).to_have_value("webm to gif")
    dialog.locator('[data-dialog-field="target_url"]').fill("https://convertio.co/webm-gif/")
    dialog.locator('[data-dialog-field="page_type"]').select_option("Video")
    dialog.locator('[data-dialog-field="target_url"]').press("Enter")
    expect(dialog).to_be_hidden()
    expect(picker).to_have_value("webm to gif")
    expect(_row(page, 0).locator("input[data-anchor-url]")).to_have_value(
        "https://convertio.co/webm-gif/"
    )
    keyword = Keyword.objects.get(keyword="webm to gif")
    assert (keyword.product, keyword.page_type) == (convertio, "Video")
    # Новый анкор есть и во второй ссылке.
    second = _row(page, 1).locator(".seo-picker-input")
    second.fill("webm")
    expect(_row(page, 1).locator(".seo-picker-list li[data-index]").first).to_contain_text(
        "webm to gif"
    )
    assert same_document(page)


def test_recommendation_goes_to_free_link(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    """Критерий: рекомендация — в свободную ссылку, нет свободной — в новую; запись."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/{placement.pk}/change/")
    picks = page.locator("[data-anchor-pick]")
    expect(picks).to_have_count(4)
    # Video недобрано — первым идёт ключ Video с наибольшим объёмом.
    expect(picks.first).to_contain_text("mp4 converter")
    expect(page.locator(".seo-anchor-pick-why").first).to_contain_text("Video: цель 60%")

    picks.nth(0).click()
    expect(_row(page, 0).locator(".seo-picker-input")).to_have_value("mp4 converter")
    picks.nth(1).click()
    expect(_row(page, 1).locator(".seo-picker-input")).to_have_value(
        re.compile(r"convert|mp4 to mp3")
    )
    expect(picks.nth(0)).to_have_class(re.compile("seo-anchor-picked"))
    page.locator("input[name=_save]").click()
    page.wait_for_url(re.compile(r"/admin/placements/placement/(\?.*)?$"))
    links = list(PlacementLink.objects.filter(placement=placement).order_by("link_index"))
    assert (links[0].link_index, links[0].anchor) == (1, "mp4 converter")
    assert links[0].target_url == "https://convertio.co/mp4-converter/"
    assert len(links) == 2


def test_picker_in_panel(admin_page: Page, live_server: LiveServer, placement: Placement) -> None:
    """Размещение панелью справа: блок анкоров и поле с поиском работают и там."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    page.locator("#result_list a", has_text="blog.com").click()
    panel = page.locator(PANEL)
    expect(panel.locator("[data-anchor-block]")).to_be_visible()
    panel.locator("[data-anchor-pick]").first.click()
    picker = panel.locator("#links-0 .seo-picker-input")
    expect(picker).to_have_value("mp4 converter")
    expect(panel.locator("#links-0 input[data-anchor-url]")).to_have_value(
        "https://convertio.co/mp4-converter/"
    )


def test_share_edited_in_place(
    admin_page: Page, live_server: LiveServer, convertio: Product
) -> None:
    """Критерий: доля правится прямо в таблице «Анкоров», без перезагрузки."""
    page = admin_page
    page.goto(f"{live_server.url}/admin/keywords/keywordcoverage/")
    mark(page)
    expect(page.locator("#content h1")).to_contain_text("Анкоры Convertio")
    target = page.locator('input[data-kind="type"][data-id="Video"][data-field="target"]')
    expect(target).to_have_value("60")
    with page.expect_response(lambda response: "/share/" in response.url):
        target.fill("55")
        target.press("Enter")
    expect(page.locator(".seo-anchors tfoot").first).to_contain_text("95%")
    assert PageTypeShare.objects.get(product=convertio, page_type="Video").target_pct == D(55)
    assert same_document(page)
