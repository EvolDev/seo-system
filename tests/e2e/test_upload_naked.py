"""CSV безанкорки: выбор состава, ручная доля, запись и повтор без дублей."""

from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.keywords.models import AnchorType, Keyword
from apps.sites.models import Product, Upload, UploadStatus
from apps.sites.uploads import anchors, service

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


def test_explicit_naked_upload_with_manual_share(admin_page: Page, live_server: LiveServer) -> None:
    product = Product.objects.create(name="Convertio test", domain="convertio.co")
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/?kind=anchors&product={product.pk}")
    expect(page.locator("#id_anchor_mode")).to_be_visible()
    page.locator("#id_anchor_mode").select_option("naked")
    page.locator("#id_prices_date").fill("2026-10-08")
    page.locator("#id_file").set_input_files(
        {
            "name": "naked.csv",
            "mimeType": "text/csv",
            "buffer": (
                b"Keyword,URL,Weight\n"
                b"https://convertio.co/,https://convertio.co/,8.68\n"
                b"Convertio test,https://convertio.co/,56.86\n"
                b"here,https://convertio.co/,1.13\n"
            ),
        }
    )
    page.get_by_role("button", name="Дальше", exact=True).click()
    expect(page.locator("#id_anchor_mode")).to_have_value("naked")
    share = page.locator('select[name="field:weight"]')
    expect(share).to_be_visible()
    share.select_option("share")
    expect(page.locator('input[name="load:weight"]')).to_be_checked()
    share.select_option("skip")
    expect(page.locator('input[name="load:weight"]')).not_to_be_checked()
    share.select_option("share")
    page.get_by_role("button", name="Дальше: сводка", exact=True).click()
    expect(page.get_by_text("Безанкорка: Бренд", exact=False)).to_be_visible()
    expect(page.get_by_text("Безанкорка: Голый адрес", exact=False)).to_be_visible()
    expect(page.get_by_text("Безанкорка: Нейтральное слово", exact=False)).to_be_visible()
    page.screenshot(path="/app/.naked-previews/summary.png", full_page=True, animations="disabled")
    page.get_by_role("button", name="Записать в базу", exact=True).click()
    expect(page.get_by_role("link", name="К анкорам", exact=True)).to_be_visible()
    upload = Upload.objects.latest("pk")
    assert upload.status == UploadStatus.DONE
    assert anchors.mode(upload.mapping) == anchors.Mode.NAKED
    assert Keyword.objects.filter(product=product).count() == 3
    assert Keyword.objects.get(product=product, keyword="here").share == Decimal("1.13")
    assert Keyword.objects.get(product=product, keyword="here").anchor_type == AnchorType.GENERIC
    assert service.anchors_plan(upload).nothing
    page.screenshot(path="/app/.naked-previews/done.png", full_page=True, animations="disabled")


def test_naked_selector_tracks_upload_kind(admin_page: Page, live_server: LiveServer) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/?kind=anchors")
    expect(page.locator("#id_anchor_mode")).to_be_visible()
    page.locator('input[name="kind"][value="price_list"]').check()
    expect(page.locator("#id_anchor_mode")).to_be_hidden()
    page.locator('input[name="kind"][value="anchors"]').check()
    expect(page.locator("#id_anchor_mode")).to_be_visible()
    page.screenshot(path="/app/.naked-previews/new.png", full_page=True, animations="disabled")
