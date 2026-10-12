"""Кнопки активности, массовые действия и полный список при загрузке."""

from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.keywords.models import AnchorType, Keyword
from apps.sites.models import Product, Upload, UploadStatus
from apps.sites.uploads import anchors
from apps.workspace.products import choose_product

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture(autouse=True)
def uploads_dir(settings: Any, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"


@pytest.fixture
def product(admin_user: User) -> Product:
    product = Product.objects.create(name="Activity Convertio", domain="convertio.co")
    choose_product(admin_user, product)
    return product


def create(product: Product, text: str, *, active: bool = True, naked: bool = False) -> Keyword:
    return Keyword.objects.create(
        product=product,
        keyword=text,
        target_url="https://convertio.co/",
        is_active=active,
        anchor_type=AnchorType.BRANDED if naked else AnchorType.EXACT,
    )


def test_single_and_bulk_activity(
    admin_page: Page, live_server: LiveServer, product: Product
) -> None:
    a = create(product, "present")
    b = create(product, "another")
    page = admin_page
    page.goto(f"{live_server.url}/admin/keywords/keywordcoverage/?product={product.pk}")
    row = page.locator("#result_list tbody tr", has_text="present")
    row.get_by_role("button", name="Отключить", exact=True).click()
    expect(page.locator("#result_list tbody")).not_to_contain_text("present")
    page.get_by_role("link", name="Неактивные (1)", exact=True).click()
    expect(page.locator("#result_list tbody")).to_contain_text("present")
    page.screenshot(
        path="/app/test-results/activity-previews/inactive.png",
        full_page=True,
        animations="disabled",
    )
    page.get_by_role("button", name="Включить", exact=True).click()
    expect(page.get_by_role("link", name="Неактивные (0)", exact=True)).to_be_visible()
    page.goto(f"{live_server.url}/admin/keywords/keywordcoverage/?product={product.pk}")
    page.locator(f'input[name="_selected_action"][value="{a.pk}"]').check()
    page.locator("select[name=action]").first.select_option("deactivate_selected")
    page.locator("button[name=index]").first.click()
    expect(page.locator("#result_list tbody")).not_to_contain_text("present")
    page.get_by_role("link", name="Неактивные (1)", exact=True).click()
    page.locator(f'input[name="_selected_action"][value="{a.pk}"]').check()
    page.locator("select[name=action]").first.select_option("activate_selected")
    page.locator("button[name=index]").first.click()
    expect(page.get_by_role("link", name="Неактивные (0)", exact=True)).to_be_visible()
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.is_active and b.is_active


@pytest.mark.parametrize("naked", [False, True])
def test_complete_list_upload(
    admin_page: Page, live_server: LiveServer, product: Product, naked: bool
) -> None:
    missing = create(product, "old same kind", naked=naked)
    opposite = create(product, "keep other kind", naked=not naked)
    present = create(product, "present", active=False, naked=naked)
    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/add/?kind=anchors&product={product.pk}")
    page.locator("#id_anchor_mode").select_option("naked" if naked else "auto")
    page.locator("#id_anchor_policy").select_option("sync")
    page.locator("#id_prices_date").fill("2026-10-09")
    page.locator("#id_file").set_input_files(
        {
            "name": "anchors.csv",
            "mimeType": "text/csv",
            "buffer": b"Keyword,URL,Weight\npresent,https://convertio.co/,50\n",
        }
    )
    page.get_by_role("button", name="Дальше", exact=True).click()
    expect(page.locator("#id_anchor_policy")).to_have_value("sync")
    # Unknown Weight deliberately opens mapping so the selected policy is checked there.
    page.locator('select[name="field:weight"]').select_option("share" if naked else "skip")
    page.get_by_role("button", name="Дальше: сводка", exact=True).click()
    expect(
        page.get_by_role("heading", name="Есть в системе, в файле нет — станут неактивными")
    ).to_be_visible()
    expect(page.locator(".seo-stat", has_text="станут неактивными")).to_contain_text("1")
    expect(page.locator(".seo-stat", has_text="снова включатся")).to_contain_text("1")
    page.screenshot(
        path=f"/app/test-results/activity-previews/summary-{naked}.png",
        full_page=True,
        animations="disabled",
    )
    page.get_by_role("button", name="Записать в базу", exact=True).click()
    expect(page.get_by_role("link", name="К анкорам", exact=True)).to_be_visible()
    for keyword in [missing, opposite, present]:
        keyword.refresh_from_db()
    assert not missing.is_active and opposite.is_active and present.is_active
    upload = Upload.objects.latest("pk")
    assert (
        upload.status == UploadStatus.DONE and anchors.policy(upload.mapping) == anchors.Policy.SYNC
    )
    page.screenshot(
        path=f"/app/test-results/activity-previews/done-{naked}.png",
        full_page=True,
        animations="disabled",
    )
