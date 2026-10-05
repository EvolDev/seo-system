"""Цены из файла размещений — через разбор, как у прайса (ADR-060)."""

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.sites.models import (
    MetricSource,
    Product,
    ReviewGroup,
    Seller,
    Site,
    SitePrice,
    Upload,
    UploadItem,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_write
from apps.sites.uploads import service
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

FILE_DATE = dt.date(2026, 10, 4)
HEADERS = ["Target", "Source", "URL статьи", "Статус", "Дата размещения", "Итог цена"]
ROW = {
    "Target": "tomsguide.com",
    "Source": "Athena Smith",
    "URL статьи": "https://www.tomsguide.com/ai-video",
    "Статус": "Размещено",
    "Дата размещения": "14.09.2026",
    "Итог цена": "200",
}


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def collaborator() -> Seller:
    return Seller.objects.get(is_collaborator=True)  # заводит миграция


def _written(product: Product, rows: Sequence[Mapping[str, str]], name: str = "a.csv") -> Upload:
    lines = [";".join(HEADERS)] + [";".join(r.get(h, "") for h in HEADERS) for r in rows]
    created = service.create_upload(
        SimpleUploadedFile(name, ("\n".join(lines) + "\n").encode("utf-8")),
        kind=UploadKind.PLACEMENTS,
        seller=None,
        product=product,
        prices_date=FILE_DATE,
    )
    upload = created.upload
    if service.needs_questions(upload):
        assert service.confirm_mapping(upload, upload.mapping or {}, "EUR") == []
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk, "all")
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


def _with_collaborator_price(seller: Seller, cents: int) -> Site:
    site = Site.objects.create(domain="tomsguide.com")
    price = SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_cents=cents,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 10, 2)),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    return site


def test_cheaper_reseller_price_waits_for_decision(clideo: Product, collaborator: Seller) -> None:
    site = _with_collaborator_price(collaborator, 30000)
    working = site.price_id
    upload = _written(clideo, [ROW])
    site.refresh_from_db()
    assert site.price_id == working  # рабочая — по-прежнему Collaborator
    item = UploadItem.objects.get(upload=upload)
    assert item.review_group == ReviewGroup.CHEAPER
    assert item.needs_decision
    assert item.ref_price_id == working
    assert item.price.placement_cents == 20000
    assert item.price.reviewed_at is None  # вопрос и в «Площадках»
    assert upload.result is not None
    assert upload.result["offers_review"] == 1


def test_site_without_price_takes_it(clideo: Product) -> None:
    upload = _written(clideo, [ROW])
    site = Site.objects.get(domain="tomsguide.com")
    item = UploadItem.objects.get(upload=upload)
    assert site.price_id == item.price_id
    assert item.review_group == ReviewGroup.NEW
    assert not item.needs_decision


def test_same_price_other_date_is_not_duplicated(clideo: Product, collaborator: Seller) -> None:
    _with_collaborator_price(collaborator, 30000)
    _written(clideo, [ROW])
    second = _written(clideo, [{**ROW, "Дата размещения": "20.09.2026"}], "b.csv")
    assert SitePrice.objects.filter(seller__name="Athena Smith").count() == 1
    item = UploadItem.objects.get(upload=second)
    assert item.review_group == ReviewGroup.SAME
    assert not item.needs_decision


def test_zero_price_is_not_an_offer(clideo: Product) -> None:
    upload = _written(clideo, [{**ROW, "Итог цена": "0"}])
    assert not SitePrice.objects.exists()
    assert not UploadItem.objects.filter(upload=upload).exists()


def test_screens_show_review(clideo: Product, collaborator: Seller, admin_client: Client) -> None:
    _with_collaborator_price(collaborator, 30000)
    upload = _written(clideo, [ROW])
    summary = admin_client.get(reverse("admin:sites_upload_summary", args=[upload.pk]))
    assert "Разбор цен — ждут 1" in summary.content.decode()
    page = admin_client.get(reverse("admin:sites_upload_review", args=[upload.pk]))
    assert page.status_code == 200
    assert "tomsguide.com" in page.content.decode()
    listing = admin_client.get(reverse("admin:sites_upload_changelist"))
    assert "ждёт разбора" in listing.content.decode()
