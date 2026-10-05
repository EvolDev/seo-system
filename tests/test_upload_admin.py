"""Экран «Загрузки»: путь через админку, разбор без перезагрузки (E1-08, ADR-044)."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.observability.models import TaskRun
from apps.sites.models import (
    ExchangeRate,
    MetricSource,
    Product,
    ReviewGroup,
    Seller,
    Site,
    SiteNote,
    SitePrice,
    Upload,
    UploadItem,
    UploadStatus,
)
from apps.sites.uploads import catalog
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

PRICE_DATE = dt.date(2026, 10, 1)
CSV = "Website,GP Price,DA\nknown.com,$150,30\nnew.com,$90,20\n"


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


@pytest.fixture
def linkhub() -> Seller:
    ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.1355"))
    return Seller.objects.create(name="LinkHub Media", currency="USD")


@pytest.fixture
def known() -> Site:
    Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="known.com")
    price = SitePrice.objects.create(
        site=site,
        seller=Seller.collaborator(),
        placement_cents=20000,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 9, 27)),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    return site


def _post_file(client: Client, content: str, **data: object) -> Any:
    file = SimpleUploadedFile("linkhub.csv", content.encode("utf-8"))
    payload = {"prices_date": PRICE_DATE.isoformat(), "file": file, **data}
    return client.post(reverse("admin:sites_upload_add"), payload)


def _uploaded(admin_client: Client, linkhub: Seller) -> Upload:
    """Прайс прошёл все шаги и записан."""
    _post_file(admin_client, CSV, kind="price_list", seller=linkhub.pk)
    upload = Upload.objects.get()
    columns = upload.columns or []
    fields = {f"field:{c['key']}": c["field"] for c in columns}
    # Галочка «грузить» у каждой колонки — как её ставит форма шага «Колонки».
    fields |= {f"load:{c['key']}": "1" for c in columns}
    admin_client.post(
        reverse("admin:sites_upload_columns", args=[upload.pk]),
        {**fields, "currency": "USD", "header_row": upload.header_row},
    )
    admin_client.post(reverse("admin:sites_upload_write", args=[upload.pk]))
    upload.refresh_from_db()
    return upload


class TestSteps:
    def test_list_has_upload_button(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:sites_upload_changelist"))
        assert response.status_code == 200
        assert "Загрузить файл" in response.content.decode()

    def test_upload_menu_and_home_card(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:index"))
        assert "предложений ждут разбора" in response.content.decode()

    def test_new_price_list_asks_about_columns(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        response = admin_client.get(reverse("admin:sites_upload_add"))
        assert response.status_code == 200
        response = _post_file(admin_client, CSV, kind="price_list", seller=linkhub.pk)
        upload = Upload.objects.get()
        assert response["Location"] == reverse("admin:sites_upload_columns", args=[upload.pk])
        page = admin_client.get(response["Location"]).content.decode()
        assert "Website" in page and "GP Price" in page and "Прочие данные" in page

    def test_unchecked_column_is_not_loaded(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        """Снятая галочка «грузить» — колонка не попадает в загрузку (просьба 05.10.2026)."""
        _post_file(admin_client, CSV, kind="price_list", seller=linkhub.pk)
        upload = Upload.objects.get()
        page = admin_client.get(reverse("admin:sites_upload_columns", args=[upload.pk]))
        assert 'name="load:' in page.content.decode()
        columns = upload.columns or []
        skipped = next(c for c in columns if c["header"] == "DA")
        payload: dict[str, object] = {f"field:{c['key']}": c["field"] for c in columns}
        payload |= {f"load:{c['key']}": "1" for c in columns if c["key"] != skipped["key"]}
        admin_client.post(
            reverse("admin:sites_upload_columns", args=[upload.pk]),
            {**payload, "currency": "USD", "header_row": upload.header_row},
        )
        upload.refresh_from_db()
        assert (upload.mapping or {})[skipped["key"]] == "skip"

    def test_new_seller_is_created_from_the_form(self, admin_client: Client, known: Site) -> None:
        ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.1"))
        _post_file(
            admin_client,
            CSV,
            kind="price_list",
            new_seller="Zain  Mediax",
            new_seller_currency="USD",
        )
        seller = Seller.objects.get(name="Zain Mediax")
        assert seller.currency == "USD"
        assert Upload.objects.get().seller == seller

    def test_seller_is_required_for_price_list(self, admin_client: Client) -> None:
        response = _post_file(admin_client, CSV, kind="price_list")
        assert "Выберите продавца" in response.content.decode()
        assert not Upload.objects.exists()

    def test_full_path_to_review(self, admin_client: Client, linkhub: Seller, known: Site) -> None:
        upload = _uploaded(admin_client, linkhub)
        assert upload.status == UploadStatus.DONE, upload.error
        assert {run.task_name for run in TaskRun.objects.all()} >= {"upload_check", "upload_write"}
        # Разметка запомнилась у продавца.
        linkhub.refresh_from_db()
        assert linkhub.column_map == {
            "website": "domain",
            "gp price": "guest_post_price",
            "da": "extra",
        }
        response = admin_client.get(reverse("admin:sites_upload_review", args=[upload.pk]))
        page = response.content.decode()
        assert response.status_code == 200
        # known.com: $150 ≈ €132 дешевле €200 у Collaborator — первая вкладка с решениями.
        assert "Дешевле рабочей" in page and "known.com" in page
        assert "Сделать рабочей" in page

    def test_summary_page_shows_counts(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        _post_file(admin_client, CSV, kind="price_list", seller=linkhub.pk)
        upload = Upload.objects.get()
        columns = upload.columns or []
        admin_client.post(
            reverse("admin:sites_upload_columns", args=[upload.pk]),
            {
                **{f"field:{c['key']}": c["field"] for c in columns},
                **{f"load:{c['key']}": "1" for c in columns},
                "currency": "USD",
            },
        )
        response = admin_client.get(reverse("admin:sites_upload_summary", args=[upload.pk]))
        page = response.content.decode()
        assert "Сводка до записи" in page
        assert "новых для базы" in page and "Записать в базу" in page
        state = admin_client.get(reverse("admin:sites_upload_state", args=[upload.pk])).json()
        assert state["status"] == UploadStatus.CHECKED and not state["busy"]

    def test_same_file_again_opens_review(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        upload = _uploaded(admin_client, linkhub)
        response = _post_file(admin_client, CSV, kind="price_list", seller=linkhub.pk)
        assert response["Location"] == reverse("admin:sites_upload_review", args=[upload.pk])
        assert Upload.objects.count() == 1

    def test_catalog_goes_straight_to_summary(self, admin_client: Client, known: Site) -> None:
        headers = list(catalog.REQUIRED)
        row = {h: "" for h in headers} | {
            "Domain": "https://www.fresh.com",
            "Publishing price article, EUR": "90.00",
            "Link type article": "dofollow",
        }
        content = ";".join(headers) + "\n" + ";".join(row[h] for h in headers) + "\n"
        file = SimpleUploadedFile("collaborator.csv", content.encode("utf-8-sig"))
        response = admin_client.post(
            reverse("admin:sites_upload_add"),
            {"kind": "collaborator_catalog", "prices_date": PRICE_DATE.isoformat(), "file": file},
        )
        upload = Upload.objects.get()
        assert upload.get_seller().is_collaborator
        assert response["Location"] == reverse("admin:sites_upload_summary", args=[upload.pk])
        assert upload.status == UploadStatus.CHECKED


class TestReviewDecisions:
    def _decide(self, client: Client, upload: Upload, action: str, items: list[int]) -> dict:  # type: ignore[type-arg]
        response = client.post(
            reverse("admin:sites_upload_decide", args=[upload.pk]),
            data=json.dumps({"action": action, "items": items}),
            content_type="application/json",
        )
        assert response.status_code == 200
        return response.json()  # type: ignore[no-any-return]

    def test_fix_then_undo(self, admin_client: Client, linkhub: Seller, known: Site) -> None:
        upload = _uploaded(admin_client, linkhub)
        item = UploadItem.objects.get(upload=upload, review_group=ReviewGroup.CHEAPER)
        old_price = known.price_id

        result = self._decide(admin_client, upload, "fix", [item.pk])
        assert result["rows"][str(item.pk)] == {"state": "working", "label": "✓ рабочая цена"}
        assert (result["done"], result["need"]) == (1, 1)
        known.refresh_from_db()
        assert known.price_id == item.price_id
        assert SiteNote.objects.filter(site=known, body__startswith="Цена: ").exists()

        response = admin_client.post(
            reverse("admin:sites_upload_undo", args=[upload.pk]),
            data=json.dumps({"undo": result["undo"]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["rows"][str(item.pk)]["state"] == "pending"
        known.refresh_from_db()
        assert known.price_id == old_price
        assert SitePrice.objects.get(pk=item.price_id).reviewed_at is None

    def test_keep(self, admin_client: Client, linkhub: Seller, known: Site) -> None:
        upload = _uploaded(admin_client, linkhub)
        item = UploadItem.objects.get(upload=upload, review_group=ReviewGroup.CHEAPER)
        result = self._decide(admin_client, upload, "keep", [item.pk])
        assert result["rows"][str(item.pk)]["state"] == "kept"
        known.refresh_from_db()
        assert known.price_id != item.price_id

    def test_bad_request(self, admin_client: Client, linkhub: Seller, known: Site) -> None:
        upload = _uploaded(admin_client, linkhub)
        response = admin_client.post(
            reverse("admin:sites_upload_decide", args=[upload.pk]),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_new_tab_and_issues_tab(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        upload = _uploaded(admin_client, linkhub)
        url = reverse("admin:sites_upload_review", args=[upload.pk])
        page = admin_client.get(url + "?tab=new&sort=dr").content.decode()
        assert "new.com" in page and "стала рабочей сама" in page
        page = admin_client.get(url + "?tab=issues").content.decode()
        assert "Дубли в файле" in page
