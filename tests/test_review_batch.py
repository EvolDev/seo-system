"""Решения разбора пачкой: «площадка на сегодня» — одним пересчётом (17.11.2026).

На проде решение по 500 строкам «дороже рабочей» пересчитывало копию после
каждой строки: 148 мс на площадку — 74 с, браузер не дожидался ответа. Теперь
`latest.deferred()` копит площадки и пересчитывает их в конце одним запросом.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.sites import latest
from apps.sites.models import (
    ExchangeRate,
    MetricSource,
    Product,
    ReviewGroup,
    Seller,
    Site,
    SiteLatest,
    SitePrice,
    Upload,
    UploadItem,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import review, service
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

PRICE_DATE = dt.date(2026, 11, 17)
DOMAINS = [f"site{n}.com" for n in range(5)]


def _refreshes(ctx: CaptureQueriesContext) -> int:
    return sum("INSERT INTO site_latest" in query["sql"] for query in ctx.captured_queries)


# ---------- latest.deferred ----------


def test_deferred_refreshes_once_at_the_end() -> None:
    sites = [Site.objects.create(domain=domain) for domain in DOMAINS]
    with CaptureQueriesContext(connection) as ctx, latest.deferred():
        for site in sites:
            assert latest.refresh([site.pk]) == 0  # только запомнила
        with latest.deferred():  # вложенный копит во внешний
            latest.refresh([sites[0].pk])
        assert _refreshes(ctx) == 0
    assert _refreshes(ctx) == 1
    assert SiteLatest.objects.filter(site_id__in=[s.pk for s in sites]).count() == len(sites)


def test_deferred_refreshes_what_was_written_before_an_error() -> None:
    site = Site.objects.create(domain="a.com")
    SiteLatest.objects.filter(site_id=site.pk).delete()
    with pytest.raises(RuntimeError), latest.deferred():
        latest.refresh([site.pk])
        raise RuntimeError("строка разбора упала")
    assert SiteLatest.objects.filter(site_id=site.pk).exists()
    # После блока — снова сразу.
    with CaptureQueriesContext(connection) as ctx:
        latest.refresh([site.pk])
    assert _refreshes(ctx) == 1


# ---------- Разбор ----------


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def upload() -> Upload:
    """Пять площадок с рабочей ценой Collaborator €200; продавец предлагает $150 — дешевле."""
    Product.objects.create(name="Convertio", domain="convertio.co")
    for domain in DOMAINS:
        site = Site.objects.create(domain=domain)
        site.price = SitePrice.objects.create(
            site=site,
            seller=Seller.collaborator(),
            placement_cents=20000,
            source=MetricSource.CSV_IMPORT,
            checked_at=start_of_day(dt.date(2026, 9, 27)),
            reviewed_at=timezone.now(),
        )
        site.save(update_fields=["price"])
    ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.16"))
    seller = Seller.objects.create(name="Sites Catalog", currency="USD")
    content = "Website,GP Price\n" + "".join(f"{domain},$150\n" for domain in DOMAINS)
    created = service.create_upload(
        SimpleUploadedFile("sites.csv", content.encode("utf-8")),
        kind=UploadKind.PRICE_LIST,
        seller=seller,
        prices_date=PRICE_DATE,
    ).upload
    if service.needs_questions(created):
        assert service.confirm_mapping(created, created.mapping or {}, "USD") == []
    service.start(created, UploadStatus.CHECKING)
    upload_check.delay(created.pk)
    service.start(created, UploadStatus.WRITING)
    upload_write.delay(created.pk)
    created.refresh_from_db()
    assert created.status == UploadStatus.DONE, created.error
    return created


def _items(upload: Upload) -> list[UploadItem]:
    items = list(UploadItem.objects.filter(upload=upload, review_group=ReviewGroup.CHEAPER))
    assert len(items) == len(DOMAINS)
    return items


def _pending_marks(items: list[UploadItem]) -> set[bool | None]:
    rows = SiteLatest.objects.filter(site_id__in=[item.site_id for item in items])
    return {row.cheaper_pending for row in rows}


@pytest.mark.parametrize("action", ["keep", "fix"])
def test_decision_refreshes_the_copy_once(upload: Upload, action: str) -> None:
    items = _items(upload)
    assert _pending_marks(items) == {True}
    with CaptureQueriesContext(connection) as ctx:
        undo, problems = review.decide(upload, [i.pk for i in items], action, author=None)
    assert problems == []
    assert _refreshes(ctx) == 1
    if action == "keep":
        assert _pending_marks(items) == {False}  # разобрано — и в копии
    else:
        sites = Site.objects.filter(pk__in=[i.site_id for i in items])
        assert {site.price_id for site in sites} == {item.price_id for item in items}

    with CaptureQueriesContext(connection) as ctx:
        review.undo(undo, author=None)
    assert _refreshes(ctx) == 1
    # Отмена вернула «не разобрано» — копия это видит, а не ждёт ночного пересчёта.
    assert _pending_marks(items) == {True}


def test_block_refreshes_the_copy_once(upload: Upload) -> None:
    items = _items(upload)
    with CaptureQueriesContext(connection) as ctx:
        review.decide(upload, [i.pk for i in items], "block", author=None)
    assert _refreshes(ctx) == 1
    assert _pending_marks(items) == {False}
