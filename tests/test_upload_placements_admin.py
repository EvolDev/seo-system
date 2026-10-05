"""Экран «Загрузки» для файла размещений: форма, колонки, сводка, запись (E1-09, ADR-051)."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.observability.models import Check, CheckStatus, Performer
from apps.placements.models import Placement
from apps.sites.models import Product, Seller, Site, Upload, UploadKind, UploadStatus
from apps.workspace.products import working_product_id

pytestmark = pytest.mark.django_db

FILE_DATE = dt.date(2026, 10, 4)
CSV = (
    "Target;Person;Source;URL статьи;Статус;Итог цена\n"
    "a.com;Evgeniy;Athena Smith;https://a.com/post;Размещено;311,81\n"
    "b.com;Lilith;Athena Smith;https://b.com/x;;120\n"
)


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


@pytest.fixture
def clideo() -> Product:
    Product.objects.create(name="Convertio", domain="convertio.co")
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _post_file(client: Client, content: str = CSV, name: str = "clideo.csv", **data: object) -> Any:
    file = SimpleUploadedFile(name, content.encode("utf-8"))
    payload = {"prices_date": FILE_DATE.isoformat(), "file": file, **data}
    return client.post(reverse("admin:sites_upload_add"), payload)


def _confirm_columns(client: Client, upload: Upload, **overrides: str) -> Any:
    fields = {f"field:{c['key']}": c["field"] for c in upload.columns or []}
    fields.update({f"field:{key}": value for key, value in overrides.items()})
    return client.post(
        reverse("admin:sites_upload_columns", args=[upload.pk]),
        {**fields, "currency": "EUR", "header_row": upload.header_row},
    )


class TestForm:
    def test_product_is_required(self, admin_client: Client, clideo: Product) -> None:
        response = _post_file(admin_client, kind="placements")
        assert response.status_code == 200
        assert "Выберите продукт" in response.content.decode()
        assert not Upload.objects.exists()

    def test_from_whom_is_optional_and_new_employee_has_no_login(
        self, admin_client: Client, clideo: Product
    ) -> None:
        response = _post_file(
            admin_client, kind="placements", product=clideo.pk, new_employee="Artem"
        )
        upload = Upload.objects.get()
        assert (upload.kind, upload.product, upload.seller) == (UploadKind.PLACEMENTS, clideo, None)
        assert upload.employee is not None and upload.employee.first_name == "Artem"
        assert not upload.employee.has_usable_password()
        # Первый файл размещений — вопрос про колонки.
        assert response["Location"] == reverse("admin:sites_upload_columns", args=[upload.pk])

    def test_form_remembers_last_kind_and_product(
        self, admin_client: Client, clideo: Product
    ) -> None:
        page = admin_client.get(reverse("admin:sites_upload_add"))
        assert page.context["form"]["kind"].value() == "price_list"
        _post_file(admin_client, kind="placements", product=clideo.pk)
        page = admin_client.get(reverse("admin:sites_upload_add"))
        assert page.context["form"]["kind"].value() == "placements"
        # Продукт — не прошлый, а рабочий (ADR-057): не выбран — первый активный.
        assert page.context["form"]["product"].value() == working_product_id(page.wsgi_request)
        # У другого человека — своё «в прошлый раз».
        other = User.objects.create_superuser("kate", password="x")
        client = Client()
        client.force_login(other)
        assert client.get(reverse("admin:sites_upload_add")).context["form"]["kind"].value() == (
            "price_list"
        )


class TestPath:
    def test_columns_summary_write(self, admin_client: Client, clideo: Product) -> None:
        _post_file(admin_client, kind="placements", product=clideo.pk)
        upload = Upload.objects.get()
        page = admin_client.get(reverse("admin:sites_upload_columns", args=[upload.pk]))
        text = page.content.decode()
        assert "Сотрудник — кто вёл" in text and "Заплачено — итог" in text
        assert "Разметка запомнится для файлов размещений" in text
        response = _confirm_columns(admin_client, upload)
        summary_url = reverse("admin:sites_upload_summary", args=[upload.pk])
        assert response["Location"] == summary_url
        upload.refresh_from_db()
        assert upload.status == UploadStatus.CHECKED
        before = admin_client.get(summary_url).content.decode()
        assert "размещений будет создано" in before
        assert "Athena Smith" in before and "Evgeniy" in before
        # Расхождений нет — второй кнопки нет.
        assert "заменив расходящиеся" not in before

        admin_client.post(reverse("admin:sites_upload_write", args=[upload.pk]))
        upload.refresh_from_db()
        assert upload.status == UploadStatus.DONE, upload.error
        assert Placement.objects.filter(product=clideo).count() == 2
        after = admin_client.get(summary_url).content.decode()
        assert "размещений создано" in after
        assert "Clideo · размещения · 04.10.2026" in after

        # Второй файл с теми же колонками — без вопросов, сразу сводка.
        response = _post_file(admin_client, CSV + "c.com;Artem;Zain;;;\n", name="2.csv",
                              kind="placements", product=clideo.pk)  # fmt: skip
        second = Upload.objects.latest("pk")
        assert response["Location"] == reverse("admin:sites_upload_summary", args=[second.pk])

        listing = admin_client.get(reverse("admin:sites_upload_changelist")).content.decode()
        assert "Размещения Clideo" in listing

    def test_replace_is_only_for_placements(self, admin_client: Client, clideo: Product) -> None:
        seller = Seller.objects.create(name="LinkHub Media", currency="EUR")
        _post_file(admin_client, "Website,GP Price\nx.com,10\n", name="p.csv",
                   kind="price_list", seller=seller.pk)  # fmt: skip
        upload = Upload.objects.get()
        response = admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]),
            {"action": "replace"},
            HTTP_X_SEO_AJAX="1",
        )
        assert response.status_code == 400


def test_placement_panel_shows_file_extras_and_human_mark(
    admin_client: Client, clideo: Product
) -> None:
    site = Site.objects.create(domain="a.com")
    placement = Placement.objects.create(
        site=site, product=clideo, extra={"Traffic": "4 086 679", "Top Geo": "us"}
    )
    Check.objects.create(
        entity_type="placement",
        entity_id=placement.pk,
        check_type="indexation",
        status=CheckStatus.OK,
        performed_by=Performer.HUMAN,
        result={"source": "upload", "file": "clideo.xlsx", "column": "Индексация 02.09.26"},
    )
    page = admin_client.get(reverse("admin:placements_placement_change", args=[placement.pk]))
    text = page.content.decode()
    assert "Из файла размещений" in text
    assert "Traffic: <b>4 086 679</b>" in text
    assert "человек, файл «clideo.xlsx», «Индексация 02.09.26»" in text
