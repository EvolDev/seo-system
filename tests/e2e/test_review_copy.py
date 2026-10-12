"""Разбор загрузки: копирование отмеченных и всех доменов вкладки, размер страницы."""

import datetime as dt
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import Product, Seller, UploadKind, UploadStatus
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import service

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

DOMAINS = [f"site{number}.com" for number in range(60)]


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


def test_copy_marked_and_all_domains(admin_page: Page, live_server: LiveServer) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    seller = Seller.objects.create(name="LinkHub Media")
    content = "Website,Price\n" + "".join(f"{domain},100\n" for domain in DOMAINS)
    upload = service.create_upload(
        SimpleUploadedFile("hub.csv", content.encode()),
        kind=UploadKind.PRICE_LIST,
        seller=seller,
        prices_date=dt.date(2026, 10, 12),
    ).upload
    if service.needs_questions(upload):
        assert service.confirm_mapping(upload, upload.mapping or {}, "EUR") == []
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk)

    page = admin_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(f"{live_server.url}/admin/sites/upload/{upload.pk}/review/?tab=new&per_page=50")
    rows = page.locator("table[data-review] tbody tr")
    expect(rows).to_have_count(50)
    expect(page.locator(".seo-per-page b").first).to_have_text("50")

    rows.nth(0).locator("input[data-select]").check()
    rows.nth(1).locator("input[data-select]").check()
    page.get_by_role("button", name="Скопировать отмеченные").click()
    expect(page.get_by_text("Скопировано: 2 домена")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == "site0.com\nsite1.com"

    page.get_by_role("button", name="Скопировать все домены (60)").click()
    expect(page.get_by_text("Скопировано: 60 доменов")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == "\n".join(DOMAINS)
