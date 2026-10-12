"""Отключение, синхронизация и возвращение анкоров без изменения старых ссылок."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.keywords.anchors import overview
from apps.keywords.models import AnchorType, Keyword, KeywordPosition
from apps.placements.forms import PlacementLinkForm
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site, Upload, UploadKind, UploadStatus
from apps.sites.uploads import anchors, service, undo

pytestmark = pytest.mark.django_db
PAGE = reverse("admin:keywords_keywordcoverage_changelist")


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture(autouse=True)
def uploads_dir(settings: Any, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"


def make(product: Product, text: str, *, active: bool = True, naked: bool = False) -> Keyword:
    return Keyword.objects.create(
        product=product,
        keyword=text,
        target_url="https://convertio.co/",
        is_active=active,
        anchor_type=AnchorType.BRANDED if naked else AnchorType.EXACT,
        page_type="Main",
    )


def load(product: Product, data: bytes, *, sync: bool = True, naked: bool = False) -> Upload:
    upload = service.create_upload(
        SimpleUploadedFile("anchors.csv", data),
        kind=UploadKind.ANCHORS,
        seller=None,
        product=product,
        prices_date=dt.date(2026, 10, 9),
        anchor_mode=anchors.Mode.NAKED if naked else anchors.Mode.AUTO,
        anchor_policy=anchors.Policy.SYNC if sync else anchors.Policy.MERGE,
    ).upload
    assert not service.needs_questions(upload), upload.columns
    service.start(upload, UploadStatus.CHECKING)
    service.check(upload.pk)
    upload.refresh_from_db()
    return upload


def write(upload: Upload) -> Upload:
    assert upload.status == UploadStatus.CHECKED, upload.error
    service.start(upload, UploadStatus.WRITING)
    service.write(upload.pk)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


CSV = b"Keyword,URL\npresent,https://convertio.co/\n"


def test_page_inactive_and_all(admin_client: Client, product: Product) -> None:
    active = make(product, "present")
    inactive = make(product, "old", active=False)
    cases: list[tuple[dict[str, str], list[int]]] = [
        ({}, [active.pk]),
        ({"activity": "inactive"}, [inactive.pk]),
        ({"activity": "all"}, [active.pk, inactive.pk]),
    ]
    for params, ids in cases:
        response = admin_client.get(PAGE, {"product": str(product.pk), **params})
        assert [row.pk for row in response.context["cl"].result_list] == ids
    html = admin_client.get(PAGE, {"product": product.pk}).content.decode()
    assert "Неактивные (1)" in html and "Отключить" in html
    assert "deactivate_selected" in html and "activate_selected" in html


def test_single_toggle_preserves_history_and_excludes_statistics(
    admin_client: Client, product: Product
) -> None:
    keyword = make(product, "present")
    KeywordPosition.objects.create(keyword=keyword, position=7)
    placement = Placement.objects.create(
        site=Site.objects.create(domain="history.example"),
        product=product,
        status=PlacementStatus.PLACED,
    )
    link = PlacementLink.objects.create(
        placement=placement, keyword=keyword, anchor=keyword.keyword, target_url=keyword.target_url
    )
    assert overview(product.pk).placed == 1
    url = reverse("admin:keywords_anchor_activity", args=[keyword.pk])
    response = admin_client.post(url + "?next=https://evil.example/", {"active": "0"})
    assert response.status_code == 302 and response["Location"].startswith(PAGE)
    keyword.refresh_from_db()
    link.refresh_from_db()
    assert not keyword.is_active and link.keyword_id == keyword.pk and link.anchor == "present"
    assert keyword.positions.count() == 1
    data = overview(product.pk)
    assert (data.placed, data.waiting, data.loose_placed, len(data.anchors)) == (0, 0, 0, 0)
    fresh = PlacementLinkForm(data={"keyword": keyword.pk, "target_url": keyword.target_url})
    assert not fresh.is_valid()
    existing = PlacementLinkForm(
        instance=link, data={"keyword": keyword.pk, "target_url": keyword.target_url}
    )
    assert existing.is_valid(), existing.errors
    assert admin_client.post(url, {"active": "1"}).status_code == 302
    assert overview(product.pk).placed == 1
    assert admin_client.get(url).status_code == 405
    assert admin_client.post(url, {"active": "bad"}).status_code == 400


def test_bulk_actions_scope_selection(admin_client: Client, product: Product) -> None:
    a = make(product, "a")
    b = make(product, "b")
    for action, activity, value in [
        ("deactivate_selected", "active", False),
        ("activate_selected", "inactive", True),
    ]:
        response = admin_client.post(
            PAGE + f"?product={product.pk}&activity={activity}",
            {"action": action, "_selected_action": [a.pk], "index": "0", "select_across": "0"},
        )
        assert response.status_code == 302
        a.refresh_from_db()
        b.refresh_from_db()
        assert a.is_active == value and b.is_active


def test_no_edit_permission(client: Client, product: Product) -> None:
    a = make(product, "a")
    viewer = User.objects.create_user("anchor_viewer", is_staff=True)
    viewer.user_permissions.add(
        *Permission.objects.filter(codename__in=["view_keyword", "view_keywordcoverage"])
    )
    client.force_login(viewer)
    assert (
        client.post(
            reverse("admin:keywords_anchor_activity", args=[a.pk]), {"active": "0"}
        ).status_code
        == 403
    )
    html = client.get(PAGE, {"product": product.pk}).content.decode()
    assert "Отключить" not in html and "deactivate_selected" not in html
    client.post(PAGE, {"action": "deactivate_selected", "_selected_action": [a.pk]})
    a.refresh_from_db()
    assert a.is_active


def test_sync_keys_preserves_naked_other_product_and_old_link(product: Product) -> None:
    present = make(product, "present", active=False)
    missing = make(product, "missing")
    brand = make(product, "Brand", naked=True)
    other = make(Product.objects.create(name="Other", domain="other.example"), "missing")
    upload = load(product, CSV)
    assert (upload.summary or {})["deactivated_total"] == 1
    assert (upload.summary or {})["reactivated_total"] == 1
    upload = write(upload)
    for keyword in [present, missing, brand, other]:
        keyword.refresh_from_db()
    assert present.is_active and not missing.is_active and brand.is_active and other.is_active
    assert (upload.result or {})["counts"]["deactivated"] == 1
    assert (upload.result or {})["counts"]["reactivated"] == 1
    service.prepare(upload)
    assert anchors.policy(upload.mapping) == anchors.Policy.SYNC


def test_sync_naked_does_not_disable_keys(product: Product) -> None:
    key = make(product, "key")
    present = make(product, "Convertio", active=False, naked=True)
    missing = make(product, "Old Brand", naked=True)
    upload = write(
        load(product, b"Keyword,URL,%\nConvertio,https://convertio.co/,50\n", naked=True)
    )
    for keyword in [key, present, missing]:
        keyword.refresh_from_db()
    assert key.is_active and present.is_active and not missing.is_active
    assert (upload.result or {})["counts"]["deactivated"] == 1
    assert (upload.summary or {})["missing"] == ["Old Brand"]


def test_merge_leaves_missing_active(product: Product) -> None:
    missing = make(product, "missing")
    upload = write(load(product, CSV, sync=False))
    missing.refresh_from_db()
    assert missing.is_active
    assert (upload.result or {})["counts"]["deactivated"] == 0


@pytest.mark.parametrize(
    "data",
    [
        b"Keyword,URL\n",
        b"Keyword,URL\npresent,bad\n",
        b"Keyword,URL\npresent,https://convertio.co/\nbad,invalid\n",
    ],
)
def test_invalid_sync_never_deactivates(product: Product, data: bytes) -> None:
    keyword = make(product, "missing")
    upload = load(product, data)
    assert upload.status == UploadStatus.FAILED and upload.error
    keyword.refresh_from_db()
    assert keyword.is_active


def test_repeated_file_reactivates_after_manual_disable(product: Product) -> None:
    upload = write(load(product, CSV))
    keyword = Keyword.objects.get(keyword="present")
    Keyword.objects.filter(pk=keyword.pk).update(is_active=False)
    repeated = load(product, CSV)
    assert repeated.pk != upload.pk
    write(repeated)
    keyword.refresh_from_db()
    assert keyword.is_active
    assert Keyword.objects.filter(product=product).count() == 1
    # Same bytes after reconciliation open the previous result, without another write.
    created = service.create_upload(
        SimpleUploadedFile("anchors.csv", CSV),
        kind=UploadKind.ANCHORS,
        seller=None,
        product=product,
        prices_date=dt.date(2026, 10, 9),
        anchor_policy=anchors.Policy.SYNC,
    )
    assert created.duplicate


def test_undo_restores_activation_flags(product: Product) -> None:
    present = make(product, "present", active=False)
    missing = make(product, "missing")
    upload = write(load(product, CSV))
    undo.undo(upload)
    present.refresh_from_db()
    missing.refresh_from_db()
    assert not present.is_active and missing.is_active


def test_mapping_policy_and_invalid_value(product: Product) -> None:
    upload = load(product, CSV)
    mapping = {"keyword": "keyword", "url": "url", anchors.POLICY_KEY: "merge"}
    assert not service.confirm_mapping(upload, mapping, "")
    assert anchors.policy(upload.mapping) == anchors.Policy.MERGE
    mapping[anchors.POLICY_KEY] = "unknown"
    assert service.confirm_mapping(upload, mapping, "")
    assert anchors.policy(upload.mapping) == anchors.Policy.MERGE


def test_full_book_syncs_both_kinds(product: Product) -> None:
    import io

    from openpyxl import Workbook

    kept = make(product, "present")
    missing = make(product, "missing")
    brand = make(product, "Convertio", active=False, naked=True)
    old_brand = make(product, "Old brand", naked=True)
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Keyword", "URL"])
    sheet.append(["present", "https://convertio.co/"])
    naked = book.create_sheet("Безанкорка")
    naked.append(["Anchor text", "URL", "%"])
    naked.append(["Convertio", "https://convertio.co/", 50])
    output = io.BytesIO()
    book.save(output)
    upload = service.create_upload(
        SimpleUploadedFile("both.xlsx", output.getvalue()),
        kind=UploadKind.ANCHORS,
        seller=None,
        product=product,
        prices_date=dt.date(2026, 10, 9),
        anchor_policy=anchors.Policy.SYNC,
    ).upload
    service.start(upload, UploadStatus.CHECKING)
    service.check(upload.pk)
    upload.refresh_from_db()
    write(upload)
    for keyword in [kept, missing, brand, old_brand]:
        keyword.refresh_from_db()
    assert kept.is_active and brand.is_active
    assert not missing.is_active and not old_brand.is_active


def test_summary_is_rebuilt_before_write(product: Product) -> None:
    upload = load(product, CSV)
    later = make(product, "added after preview")
    write(upload)
    later.refresh_from_db()
    assert not later.is_active


def test_sync_disables_all_absent_case_variants(product: Product) -> None:
    first = make(product, "Gone")
    second = make(product, "gone")
    upload = write(load(product, CSV))
    first.refresh_from_db()
    second.refresh_from_db()
    assert not first.is_active and not second.is_active
    assert (upload.result or {})["counts"]["deactivated"] == 2
