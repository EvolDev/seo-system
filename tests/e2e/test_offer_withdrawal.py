"""«Снять с продажи» на странице каталога (ADR-068): галочка, запись без перезагрузки."""

import datetime as dt
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import (
    MetricSource,
    Product,
    Seller,
    Site,
    SitePrice,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check
from apps.sites.uploads import catalog, service
from apps.sites.uploads.plan import start_of_day

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


def _catalog_csv(domains: list[str]) -> bytes:
    lines = [";".join(f'"{header}"' for header in catalog.REQUIRED)]
    for domain in domains:
        row = {
            catalog.DOMAIN: f"https://{domain}",
            catalog.COLLABORATOR_URL: f"https://collaborator.pro/{domain}",
            catalog.LANGUAGES: "English",
            catalog.LINK_TYPE: "dofollow",
            catalog.PRICE: "100",
            catalog.DR: "40",
        }
        lines.append(";".join(f'"{row.get(header, "")}"' for header in catalog.REQUIRED))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_withdraw_needs_the_full_catalog_mark(admin_page: Page, live_server: LiveServer) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    collaborator = Seller.collaborator()
    gone = Site.objects.create(domain="gone.com")
    price = SitePrice.objects.create(
        site=gone,
        seller=collaborator,
        placement_cents=12000,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 10, 1)),
        reviewed_at=timezone.now(),
    )
    gone.price = price
    gone.save(update_fields=["price"])
    file = SimpleUploadedFile("collaborator.csv", _catalog_csv(["kept.com", "other.com"]))
    upload = service.create_upload(
        file,
        kind=UploadKind.COLLABORATOR_CATALOG,
        seller=collaborator,
        prices_date=dt.date(2026, 10, 20),
    ).upload
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)

    page = admin_page
    page.goto(f"{live_server.url}/admin/sites/upload/{upload.pk}/summary/")
    panel = page.locator('[data-panel="withdraw"]')
    expect(panel.get_by_role("heading")).to_have_text("Нет в этом файле — 1")
    panel.locator("summary").click()
    expect(panel.get_by_text("gone.com", exact=False)).to_be_visible()
    button = panel.get_by_role("button", name="Снять с продажи")
    # Без галочки «полный каталог» браузер форму не отправит.
    button.click()
    expect(page.locator(".seo-toast")).to_have_count(0)
    assert not SitePrice.objects.filter(withdrawn=True).exists()
    page.wait_for_timeout(1500)
    page.screenshot(path="/app/test-results/withdraw-before.png", full_page=True)

    panel.get_by_label("Подтверждаю: файл выгружен из Collaborator целиком, без фильтров").check()
    button.click()
    expect(page.get_by_text("Сняты с продажи")).to_be_visible()
    expect(panel.get_by_role("heading")).to_have_text("Нет в этом файле — 0")
    expect(button).to_be_disabled()
    expect(page.get_by_text("снято с продажи: 1, рабочая цена снята: 1")).to_be_visible()
    page.wait_for_timeout(1500)
    page.screenshot(path="/app/test-results/withdraw-after.png", full_page=True)
    gone.refresh_from_db()
    assert gone.price_id is None
    assert SitePrice.objects.filter(site=gone, withdrawn=True).count() == 1
