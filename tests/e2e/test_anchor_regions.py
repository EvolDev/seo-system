"""Списки анкоров по регионам (ADR-075) в браузере — seo/anchors.js и seo/country-picker.js.

В карточке размещения регион угадан по площадке, в поле «Анкор» — анкоры этого
списка; сменили список в блоке — блок пришёл заново, поиск — по новому списку,
выбор сохраняется с размещением. На странице «Анкоры» список выбирается с
флагами, окно «Новый анкор» пишет в открытый список.
Серверную часть проверяют tests/test_anchor_regions.py и test_upload_anchor_regions.py.
"""

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.keywords.models import AnchorList, AnchorType, Keyword
from apps.placements.models import Placement
from apps.sites.models import Product, Site, SiteCountryMetric
from apps.workspace.products import choose_product

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture
def lists(admin_user: User) -> tuple[AnchorList, AnchorList]:
    product, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    choose_product(admin_user, product)
    us = AnchorList.objects.create(product=product, country="us")
    br = AnchorList.objects.create(product=product, country="br")
    for anchor_list, text, url in (
        (us, "mp4 to mp3", "https://convertio.co/mp4-mp3/"),
        (us, "video converter", "https://convertio.co/video-converter/"),
        (br, "mp4 to mp3", "https://convertio.co/pt/mp4-mp3/"),
        (br, "conversor de vídeo", "https://convertio.co/pt/video-converter/"),
    ):
        Keyword.objects.create(
            anchor_list=anchor_list,
            keyword=text,
            target_url=url,
            page_type="Video",
            anchor_type=AnchorType.EXACT,
        )
    return us, br


def _pick_region(container, name: str) -> None:  # type: ignore[no-untyped-def]
    container.locator(".seo-country-input").click()
    container.locator(".seo-country-list li", has_text=name).click()


def test_placement_region_guess_switch_and_save(
    admin_page: Page, live_server: LiveServer, lists: tuple[AnchorList, AnchorList]
) -> None:
    """Бразильская площадка → список «Бразилия», в поиске его анкоры; сменили на США —
    блок и поиск по США, выбор сохранился в размещении."""
    us, _ = lists
    placement = Placement.objects.create(
        site=Site.objects.create(domain="dci.com.br"), product=us.product
    )
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/{placement.pk}/change/")
    block = page.locator("[data-anchor-block]")
    region = block.locator(".seo-anchor-region")
    expect(region.locator(".seo-country-input")).to_have_value("Бразилия")
    expect(block.locator("[data-anchor-region-why]")).to_have_text("по домену .br")

    # Ссылок у размещения нет — строку добавляем.
    page.locator(".inline-group .add-row a").click()
    picker = page.locator("#links-0 .seo-picker-input")
    picker.fill("mp4 to")
    picker.press("Enter")
    expect(page.locator("#links-0 input[data-anchor-url]")).to_have_value(
        "https://convertio.co/pt/mp4-mp3/"
    )
    picker.fill("conversor")
    expect(page.locator("#links-0 .seo-picker-list li[data-index]").first).to_contain_text(
        "conversor de vídeo"
    )
    picker.press("Escape")

    # Раскрытый «Рекомендуем» остаётся раскрытым, когда блок приходит заново.
    block.locator("[data-anchor-picks] > summary").click()
    _pick_region(region, "США")
    block = page.locator("[data-anchor-block]")
    expect(block.locator("[data-anchor-region-why]")).to_have_text("выбран вами")
    expect(block.locator("[data-anchor-picks]")).to_have_attribute("open", "")
    expect(block.locator("[data-anchor-region-warn]")).to_contain_text(
        "Анкор из другого списка у ссылок: 1"
    )
    picker.fill("conversor")
    expect(page.locator("#links-0 .seo-picker-list")).to_contain_text("Такого анкора нет")
    picker.fill("video conv")
    picker.press("Enter")
    expect(block.locator("[data-anchor-region-warn]")).to_be_hidden()
    page.locator("input[name=_continue]").click()
    expect(page.locator(".messagelist, .seo-toast").first).to_be_visible()
    placement.refresh_from_db()
    assert placement.anchor_list == us
    keyword = placement.links.get().keyword
    assert keyword is not None and keyword.anchor_list == us


def test_anchors_page_region_and_new_anchor(
    admin_page: Page, live_server: LiveServer, lists: tuple[AnchorList, AnchorList]
) -> None:
    """Список с флагами над страницей: выбрали «Бразилию» — её анкоры; новый анкор — в неё."""
    _, br = lists
    page = admin_page
    page.goto(f"{live_server.url}/admin/keywords/keywordcoverage/")
    head = page.locator("[data-anchors-page]")
    expect(head.locator(".seo-anchors-region .seo-country-input")).to_have_value("США")
    expect(page.locator("#result_list")).not_to_contain_text("conversor de vídeo")

    _pick_region(head.locator(".seo-anchors-region"), "Бразилия")
    expect(page.locator("#result_list")).to_contain_text("conversor de vídeo")
    head = page.locator("[data-anchors-page]")
    expect(head.locator(".seo-anchors-region .seo-country-input")).to_have_value("Бразилия")
    expect(head).to_contain_text("Удалить список «Бразилия»")

    head.locator("[data-anchor-new]").click()
    dialog = page.locator("[data-anchor-dialog]")
    expect(dialog).to_be_visible()
    expect(dialog.locator(".seo-anchor-dialog-list .seo-country-input")).to_have_value("Бразилия")
    dialog.locator('[data-dialog-field="keyword"]').fill("converter mp4")
    dialog.locator('[data-dialog-field="target_url"]').fill("https://convertio.co/pt/mp4/")
    dialog.locator("[data-anchor-dialog-save]").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#result_list")).to_contain_text("converter mp4")
    assert Keyword.objects.get(keyword="converter mp4").anchor_list == br


def test_placements_region_filter(
    admin_page: Page, live_server: LiveServer, lists: tuple[AnchorList, AnchorList]
) -> None:
    """«Размещения» → фильтр «Регион» с флагами: «Бразилия» — только бразильские (ADR-076)."""
    us, br = lists
    for domain, anchor_list in (("dci.com.br", None), ("srzd.com", br), ("blog.com", None)):
        Placement.objects.create(
            site=Site.objects.create(
                domain=domain, language="en" if domain == "blog.com" else None
            ),
            product=us.product,
            anchor_list=anchor_list,
        )
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    expect(page.locator("#result_list")).to_contain_text("blog.com")
    region = page.locator("#changelist-filter .seo-region-filter")
    expect(region.locator(".seo-country-input")).to_have_value("Все регионы")
    _pick_region(region, "Бразилия")
    expect(page.locator("#result_list")).not_to_contain_text("blog.com")
    expect(page.locator("#result_list")).to_contain_text("dci.com.br")
    expect(page.locator("#result_list")).to_contain_text("srzd.com")
    assert "region=br" in page.url


def test_export_region_columns_and_field_search(
    admin_page: Page, live_server: LiveServer, lists: tuple[AnchorList, AnchorList]
) -> None:
    """Выгрузка из «Размещений» с «Регион: Бразилия»: «Region Traff» — трафик Бразилии,
    «Region» — br; источник колонки ищется по названию (ADR-076)."""
    us, _ = lists
    brazil = Site.objects.create(domain="dci.com.br")
    usa = Site.objects.create(domain="blog.com", language="en")
    SiteCountryMetric.objects.create(site=brazil, country="br", organic_traffic=5000)
    SiteCountryMetric.objects.create(site=brazil, country="us", organic_traffic=30)
    SiteCountryMetric.objects.create(site=usa, country="us", organic_traffic=7000)
    for site in (brazil, usa):
        Placement.objects.create(site=site, product=us.product)
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/export-designer/?region=br")
    expect(page.locator("#server-status")).to_contain_text("Готово: 1 строк")

    def row(header: str):  # type: ignore[no-untyped-def]
        return page.locator(
            "#columns tr", has=page.locator(f'[data-action="header"][value="{header}"]')
        )

    expect(row("Region Traff").locator(".sample-value")).to_have_text("5000")
    expect(row("Region").locator(".sample-value")).to_have_text("br")
    page.locator("#tab-preview").click()
    expect(page.locator("#preview thead")).to_contain_text("Region Traff")
    page.locator("#tab-columns").click()

    # Источник — с поиском: «страна с наиб» находит «Страна с наибольшим трафиком».
    first = page.locator("#columns tr").first
    first.locator(".source-control .seo-picker-input").fill("страна с наиб")
    first.locator(".seo-picker-list li", has_text="Страна с наибольшим трафиком").click()
    expect(page.locator("#columns tr").first.locator('[data-action="source"]')).to_have_value(
        "top_geo"
    )
