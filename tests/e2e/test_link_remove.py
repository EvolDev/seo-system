"""⊗ у сохранённой ссылки размещения (admin/edit_inline/stacked_remove.html).

Нажали ⊗ — блок ссылки свернулся до «уберётся при сохранении · Вернуть»;
«Вернуть» — ссылка снова целиком; «Сохранить» — ссылка удалена, вторая на
месте. Всё — в панели справа, без перезагрузки.
"""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement, PlacementLink
from apps.sites.models import Product, Site

from .conftest import mark, same_document

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture
def placement() -> Placement:
    product, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    placement = Placement.objects.create(
        site=Site.objects.create(domain="opentools.ai"), product=product
    )
    placement.links.create(
        anchor="opus to mp3", target_url="https://convertio.co/opus-mp3/", link_index=1
    )
    placement.links.create(anchor="convertor", target_url="https://convertio.co/", link_index=2)
    return placement


def test_remove_undo_and_save(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    mark(page)
    page.locator("#result_list a.seo-st").click()
    panel = page.locator(".seo-panel")
    first = panel.locator(".inline-related.has_original", has_text="opus to mp3")
    anchor = first.locator("fieldset").first
    note = first.locator(".seo-inline-removed")
    expect(anchor).to_be_visible()
    expect(note).to_be_hidden()

    first.locator(".seo-inline-remove-btn").click()
    expect(anchor).to_be_hidden()
    expect(note).to_be_visible()
    expect(note).to_contain_text("уберётся при сохранении")

    note.locator("label", has_text="Вернуть").click()
    expect(anchor).to_be_visible()
    expect(note).to_be_hidden()

    first.locator(".seo-inline-remove-btn").click()
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    assert same_document(page)
    assert list(PlacementLink.objects.values_list("anchor", flat=True)) == ["convertor"]
