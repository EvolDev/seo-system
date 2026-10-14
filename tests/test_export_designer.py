"""Контракт конструктора: порядок, файлы, неопределённые строки и личные шаблоны."""

import csv
import datetime as dt
import io
import json
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from apps.placements import export_designer as design
from apps.placements.models import Placement, PlacementLink
from apps.sites.models import Product, Site
from apps.workspace.models import ExportProfile

pytestmark = pytest.mark.django_db


@pytest.fixture
def placed() -> Placement:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="example.com")
    return Placement.objects.create(
        site=site,
        product=product,
        price_paid_cents=12345,
        currency="EUR",
        ordered_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
    )


def post(client: Client, action: str, **payload: Any) -> Any:
    return client.post(
        reverse("admin:placements_placement_export_designer"),
        {"action": action, "payload": json.dumps(payload)},
    )


def config(*sources: str) -> dict[str, Any]:
    value = design.initial(design.fields())
    if sources:
        value["columns"] = [
            {
                "id": str(i),
                "index": None,
                "header": source,
                "source": source,
                "enabled": True,
                "constant": "",
            }
            for i, source in enumerate(sources)
        ]
    return value


def test_default_and_typed_excel(admin_client: Client, placed: Placement) -> None:
    value = config()
    preview = post(admin_client, "preview", config=value).json()
    assert len(preview["headers"]) == 34
    assert preview["headers"][0] == "" and preview["headers"][6] == "Дата отправки"
    assert preview["rows"][0][0] == "example.com"
    excel = post(admin_client, "download", config=value, digest=preview["digest"], format="xlsx")
    book = load_workbook(io.BytesIO(excel.content))
    assert book.sheetnames == ["Размещения"]
    assert book.active is not None
    assert book.active["G2"].value == dt.datetime(2026, 10, 1)
    assert book.active["Z2"].value == 123.45
    assert excel["Content-Type"].startswith("application/vnd.openxmlformats")


def test_renamed_reordered_fields_csv_and_formula_safety(
    admin_client: Client, placed: Placement
) -> None:
    value = config("paid", "target", "comment")
    value.update(delimiter=",", decimal=".")
    value["columns"][0]["header"] = "=1+1"
    placed.comment = '=HYPERLINK("bad")'
    placed.save()
    preview = post(admin_client, "preview", config=value).json()
    response = post(admin_client, "download", config=value, digest=preview["digest"], format="csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["'=1+1", "target", "comment"]
    assert rows[1] == ["123.45", "example.com", '\'=HYPERLINK("bad")']
    response = post(admin_client, "download", config=value, digest=preview["digest"], format="xlsx")
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert sheet is not None and sheet["A1"].data_type == "s"
    assert sheet["C2"].data_type == "s"


def test_link_slots_do_not_shift(admin_client: Client, placed: Placement) -> None:
    PlacementLink.objects.create(
        placement=placed, link_index=2, anchor="second", target_url="https://convertio.co/"
    )
    preview = post(
        admin_client, "preview", config=config("links.1.anchor", "links.2.anchor")
    ).json()
    assert preview["rows"] == [["", "second"]]


def test_changed_data_requires_new_preview(admin_client: Client, placed: Placement) -> None:
    value = config("paid")
    digest = post(admin_client, "preview", config=value).json()["digest"]
    placed.price_paid_cents = 450
    placed.save()
    response = post(admin_client, "download", config=value, digest=digest, format="xlsx")
    assert response.status_code == 409 and "изменились" in response.json()["error"]


def test_upload_preserves_blank_headers_rows_and_duplicate_domains(
    admin_client: Client, placed: Placement
) -> None:
    raw = b",Month\nexample.com,Oct-26\n,\nexample.com,Sep-26\nmissing.com,Oct-26\n"
    response = admin_client.post(
        reverse("admin:placements_placement_export_designer"),
        {"action": "import", "file": SimpleUploadedFile("sample.csv", raw)},
    )
    imported = response.json()
    value = imported["config"]
    value["columns"][1]["source"] = "original"
    preview = post(admin_client, "preview", config=value, token=imported["token"]).json()
    assert len(preview["issues"]) == 3
    assert preview["count"] == 4 and preview["headers"][0] == ""
    blocked = post(
        admin_client,
        "download",
        config=value,
        token=imported["token"],
        digest=preview["digest"],
        format="xlsx",
    )
    assert blocked.status_code == 409
    decisions = {
        "2": {"action": "match", "id": placed.pk},
        "4": {"action": "keep"},
        "5": {"action": "keep"},
    }
    preview = post(
        admin_client, "preview", config=value, token=imported["token"], decisions=decisions
    ).json()
    assert not preview["issues"] and preview["rows"][1] == ["", ""]
    response = post(
        admin_client,
        "copy",
        config=value,
        token=imported["token"],
        decisions=decisions,
        digest=preview["digest"],
        start=1,
        stop=2,
    )
    assert response.json()["rows"] == [["Oct-26"], [""], ["Sep-26"], ["Oct-26"]]


def test_import_xlsx_sheet_and_header_row(admin_client: Client, placed: Placement) -> None:
    workbook = Workbook()
    assert workbook.active is not None
    workbook.active.append(["Wrong sheet"])
    sheet = workbook.create_sheet("Размещения")
    sheet.append(["Report title"])
    sheet.append([None, "Месяц", "Месяц"])
    sheet.append(["example.com", "Oct-26", "Sep-26"])
    stream = io.BytesIO()
    workbook.save(stream)
    response = admin_client.post(
        reverse("admin:placements_placement_export_designer"),
        {
            "action": "import",
            "sheet": "1",
            "header_row": "2",
            "file": SimpleUploadedFile("book.xlsx", stream.getvalue()),
        },
    )
    result = response.json()
    assert result["sheets"] == ["Sheet", "Размещения"]
    assert [c["header"] for c in result["config"]["columns"]] == ["", "Месяц", "Месяц"]
    assert result["count"] == 1


def test_profiles_are_personal_and_revision_protected(
    admin_client: Client, placed: Placement
) -> None:
    value = config("target")
    saved = post(admin_client, "save", config=value, name="Report").json()
    assert post(admin_client, "save", config=value, name="report").status_code == 409
    user = User.objects.create_superuser("other", "other@example.com", "test")
    other = Client()
    other.force_login(user)
    assert (
        post(other, "save", config=value, name="Report", id=saved["id"], revision=1).status_code
        == 403
    )
    changed = post(admin_client, "save", config=value, name="Renamed", id=saved["id"], revision=1)
    assert changed.json()["revision"] == 2
    assert (
        post(admin_client, "save", config=value, name="Old", id=saved["id"], revision=1).status_code
        == 409
    )
    deleted = post(admin_client, "delete", id=saved["id"], revision=2).json()
    assert deleted["profiles"] == []
    assert (
        post(admin_client, "restore", id=saved["id"], revision=3).json()["profiles"][0]["name"]
        == "Renamed"
    )


def test_draft_restores_for_same_user(admin_client: Client, placed: Placement) -> None:
    value = config("target")
    assert post(admin_client, "draft", config=value).status_code == 200
    assert ExportProfile.objects.get().name == ""
    page = admin_client.get(reverse("admin:placements_placement_export_designer"))
    assert page.context["bootstrap"]["config"] == value


def test_selection_and_filter_are_preserved(admin_client: Client, placed: Placement) -> None:
    other = Placement.objects.create(
        site=Site.objects.create(domain="other.com"), product=placed.product
    )
    url = reverse("admin:placements_placement_export_designer") + "?q=example.com"
    response = admin_client.post(
        url,
        {
            "action": "preview",
            "ids": [str(other.pk)],
            "payload": json.dumps({"config": config("target")}),
        },
    )
    assert response.json()["count"] == 0


def test_unknown_fields_and_forged_template_token_rejected(
    admin_client: Client, placed: Placement
) -> None:
    assert post(admin_client, "preview", config=config("sql:select *")).status_code == 400
    assert post(admin_client, "preview", config=config(), token="wrong").status_code == 400
    response = admin_client.get(reverse("admin:placements_placement_export_designer"))
    assert response.status_code == 200


def test_constructor_keeps_admin_navigation(admin_client: Client, placed: Placement) -> None:
    response = admin_client.get(reverse("admin:placements_placement_export_designer"))
    assert response.status_code == 200
    assert response.context["is_nav_sidebar_enabled"]
    assert response.context["available_apps"]
    assert response.context["opts"] == Placement._meta
    html = response.content.decode()
    assert 'id="header"' in html and 'id="nav-sidebar"' in html
    assert 'id="palette-switch"' in html
