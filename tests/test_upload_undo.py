"""Отмена загрузки по журналу и удаление со всем, что без записи не живёт (ADR-060)."""

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.observability.models import Check
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import (
    MetricSource,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteList,
    SitePrice,
    SiteStatus,
    SiteStatusChange,
    Upload,
    UploadChange,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_write
from apps.sites.uploads import service, undo
from apps.sites.uploads.plan import start_of_day
from config import deletion

pytestmark = pytest.mark.django_db

FILE_DATE = dt.date(2026, 10, 4)
HEADERS = ["Target", "Person", "Source", "URL статьи", "Статус", "Дата размещения", "Анкор1",
           "Ссылка1", "Итог цена"]  # fmt: skip
TOMSGUIDE = {
    "Target": "tomsguide.com",
    "Person": "Evgeniy",
    "Source": "Athena Smith",
    "URL статьи": "https://www.tomsguide.com/ai-video",
    "Статус": "Размещено",
    "Дата размещения": "14.09.2026",
    "Анкор1": "video editor",
    "Ссылка1": "https://clideo.com/video-editor",
    "Итог цена": "311,81",
}


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def clideo() -> Product:
    Product.objects.create(name="Convertio", domain="convertio.co")
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _csv(rows: Sequence[Mapping[str, str]]) -> bytes:
    lines = [";".join(HEADERS)]
    lines += [";".join(row.get(header, "") for header in HEADERS) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _written(product: Product, rows: Sequence[Mapping[str, str]], name: str = "a.csv") -> Upload:
    created = service.create_upload(
        SimpleUploadedFile(name, _csv(rows)),
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


def _site_state(site: Site, product: Product) -> str:
    return str(ProductSite.objects.get(site=site, product=product).status)


class TestDeletedBetween:
    """Строку, на которую ссылалась загрузка, удалили руками до отмены.

    Оплачено 05.10.2026: пользователь снял цены неудачной загрузки в
    «Предложениях продавцов», а потом нажал «Отменить загрузку» — отмена
    возвращала площадке рабочую цену, которой уже нет, и падала с ошибкой
    внешнего ключа.
    """

    def test_undo_survives_a_deleted_working_price(self, clideo: Product) -> None:
        site = Site.objects.create(domain="tomsguide.com")
        old_price = SitePrice.objects.create(
            site=site,
            # Тот же продавец, что в файле: его цена и становится рабочей заново.
            seller=Seller.objects.create(name="Athena Smith"),
            placement_cents=9900,
            source=MetricSource.CSV_IMPORT,
            checked_at=start_of_day(dt.date(2026, 8, 1)),
            reviewed_at=timezone.now(),
        )
        site.price = old_price
        site.save(update_fields=["price"])

        upload = _written(clideo, [TOMSGUIDE])  # загрузка сменит рабочую цену
        site.refresh_from_db()
        assert site.price_id != old_price.pk

        # Цену сняли руками в «Предложениях продавцов» — там удаление идёт через config.deletion.
        deletion.delete({SitePrice: [old_price.pk]})

        undo.undo(upload)

        site.refresh_from_db()
        assert site.price_id is None  # вернуть удалённую цену нечем, но отмена прошла
        assert not Placement.objects.filter(product=clideo).exists()


class TestAnalyze:
    """После записи и отмены — `ANALYZE` тронутых таблиц.

    Оплачено 05.10.2026: после файла размещений «Площадки» открывались
    48 секунд вместо одной, пока статистика планировщика не обновилась.
    """

    def test_write_analyzes_touched_tables(self, clideo: Product) -> None:
        with CaptureQueriesContext(connection) as queries:
            _written(clideo, [TOMSGUIDE])
        assert {"placements", "sites", "site_prices"} <= _analyzed(queries)

    def test_undo_analyzes_touched_tables(self, clideo: Product) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        with CaptureQueriesContext(connection) as queries:
            undo.undo(upload)
        assert {"placements", "sites"} <= _analyzed(queries)


def _analyzed(queries: CaptureQueriesContext) -> set[str]:
    return {
        query["sql"].removeprefix("ANALYZE ").strip('"')
        for query in queries.captured_queries
        if query["sql"].startswith("ANALYZE ")
    }


class TestUndo:
    def test_new_rows_go_and_file_is_removed(
        self, clideo: Product, uploads_dir: Path, django_capture_on_commit_callbacks: Any
    ) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        assert upload.journaled
        assert UploadChange.objects.filter(upload=upload).exists()
        path = uploads_dir / upload.file_path
        assert path.exists()

        report = undo.preview(upload)
        assert "Удалится — размещения: 1" in report.lines(), report.lines()
        assert Placement.objects.count() == 1  # предпросмотр ничего не меняет

        # Файл удаляется после фиксации транзакции.
        with django_capture_on_commit_callbacks(execute=True):
            undo.undo(upload)
        assert not Upload.objects.filter(pk=upload.pk).exists()
        assert not Placement.objects.exists()
        assert not PlacementLink.objects.exists()
        assert not Check.objects.exists()
        assert not Site.all_objects.filter(domain="tomsguide.com").exists()
        assert not Seller.objects.filter(name="Athena Smith").exists()
        assert not User.objects.filter(first_name="Evgeniy").exists()
        assert not SiteList.objects.exists()
        assert not SitePrice.objects.exists()
        assert not SiteStatusChange.objects.exists()
        assert not path.exists()

    def test_same_file_can_be_loaded_again(self, clideo: Product) -> None:
        first = _written(clideo, [TOMSGUIDE])
        undo.undo(first)
        again = _written(clideo, [TOMSGUIDE])
        assert again.pk != first.pk
        assert Placement.objects.count() == 1

    def test_changed_rows_come_back(self, clideo: Product) -> None:
        # Площадка и заявка уже были: загрузка дополнила заявку и продвинула статусы.
        site = Site.objects.create(domain="tomsguide.com")
        ProductSite.objects.filter(site=site, product=clideo).update(status=SiteStatus.ORDERED)
        placement = Placement.objects.create(
            site=site, product=clideo, status=PlacementStatus.ORDERED
        )
        history_before = SiteStatusChange.objects.count()
        upload = _written(clideo, [TOMSGUIDE])
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.PLACED
        assert placement.article_url
        assert _site_state(site, clideo) == SiteStatus.PLACED

        undo.undo(upload)
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.ORDERED
        assert placement.article_url is None
        assert placement.seller_id is None
        assert placement.price_paid_cents is None
        assert _site_state(site, clideo) == SiteStatus.ORDERED
        assert Site.all_objects.filter(pk=site.pk).exists()
        # Ни строк истории загрузки, ни строк, которые записала сама отмена.
        assert SiteStatusChange.objects.count() == history_before

    def test_later_upload_on_same_rows_blocks(self, clideo: Product) -> None:
        first = _written(clideo, [TOMSGUIDE])
        second = _written(
            clideo, [{**TOMSGUIDE, "Статус": "Размещено", "Итог цена": "320"}], "b.csv"
        )
        assert [u.pk for u in undo.later_uploads(first)] == [second.pk]
        with pytest.raises(undo.Blocked):
            undo.undo(first)
        undo.undo(second)
        undo.undo(first)
        assert not Placement.objects.exists()

    def test_row_added_after_upload_goes_with_it(self, clideo: Product) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        site = Site.objects.get(domain="tomsguide.com")
        convertio = Product.objects.get(name="Convertio")
        Placement.objects.create(site=site, product=convertio, status=PlacementStatus.IN_WORK)
        report = undo.preview(upload)
        assert any("созданное после загрузки — размещения: 1" in line for line in report.lines())
        undo.undo(upload)
        assert not Placement.objects.exists()

    def test_employee_used_elsewhere_stays(self, clideo: Product) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        employee = User.objects.get(first_name="Evgeniy")
        other = Site.objects.create(domain="other.com")
        Placement.objects.create(site=other, product=clideo, employee=employee)
        report = undo.undo(upload)
        assert report.kept_users == 1
        assert User.objects.filter(pk=employee.pk).exists()

    def test_upload_before_journal_removes_only_itself(self, clideo: Product) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        UploadChange.objects.filter(upload=upload).delete()
        Upload.objects.filter(pk=upload.pk).update(journaled=False)
        upload.refresh_from_db()
        assert "до журнала" in undo.preview(upload).lines()[0]
        undo.undo(upload)
        assert not Upload.objects.filter(pk=upload.pk).exists()
        assert Placement.objects.count() == 1


class TestDeletion:
    def test_seller_takes_offers_and_clears_placements(self, clideo: Product) -> None:
        seller = Seller.objects.create(name="Reseller")
        site = Site.objects.create(domain="a.com")
        price = SitePrice.objects.create(
            site=site,
            seller=seller,
            placement_cents=100,
            source=MetricSource.CSV_IMPORT,
            checked_at=start_of_day(FILE_DATE),
            reviewed_at=timezone.now(),
        )
        site.price = price
        site.save(update_fields=["price"])
        placement = Placement.objects.create(site=site, product=clideo, seller=seller)

        plan = deletion.collect({Seller: [seller.pk]})
        texts = {line.text: line.count for line in plan.lines()}
        assert texts["Продавцы"] == 1
        assert plan.count(SitePrice) == 1
        assert any(line.cleared and line.count == 1 for line in plan.lines())

        deletion.execute(plan)
        assert not Seller.objects.filter(pk=seller.pk).exists()
        assert not SitePrice.objects.exists()
        placement.refresh_from_db()
        site.refresh_from_db()
        assert placement.seller_id is None
        assert site.price_id is None

    def test_site_takes_placements_links_and_checks(self, clideo: Product) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        site = Site.objects.get(domain="tomsguide.com")
        deletion.delete({Site: [site.pk]})
        assert not Site.all_objects.filter(pk=site.pk).exists()
        assert not Placement.objects.exists()
        assert not PlacementLink.objects.exists()
        assert not Check.objects.exists()
        assert Upload.objects.filter(pk=upload.pk).exists()


class TestAdmin:
    @pytest.fixture
    def client_admin(self) -> Client:
        user = User.objects.create_superuser("boss", password="x")
        client = Client()
        client.force_login(user)
        return client

    def test_delete_page_lists_what_goes(self, clideo: Product, client_admin: Client) -> None:
        seller = Seller.objects.create(name="Reseller")
        site = Site.objects.create(domain="a.com")
        Placement.objects.create(site=site, product=clideo, seller=seller)
        url = reverse("admin:sites_seller_delete", args=[seller.pk])
        page = client_admin.get(url).content.decode()
        assert "Удалить «Reseller»?" in page
        assert "останутся без «продавец»" in page
        client_admin.post(url, {"post": "yes"})
        assert not Seller.objects.filter(pk=seller.pk).exists()

    def test_upload_delete_is_undo(self, clideo: Product, client_admin: Client) -> None:
        upload = _written(clideo, [TOMSGUIDE])
        url = reverse("admin:sites_upload_delete", args=[upload.pk])
        page = client_admin.get(url).content.decode()
        assert "Отменить загрузку" in page
        assert "Удалится — размещения: 1" in page
        client_admin.post(url, {"post": "yes"})
        assert not Upload.objects.filter(pk=upload.pk).exists()
        assert not Placement.objects.exists()
