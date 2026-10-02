"""Разбор: «Удалить из базы» и «Заблокировать»; кнопки без перезагрузки (E1-08, ADR-044).

Удалённая площадка пропадает из базы и разбора, в следующей выгрузке может
прийти снова как новая. Заблокированная — чёрный список у всех продуктов:
пропадает из разбора, загрузки её пропускают.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    Product,
    ProductSite,
    ReviewGroup,
    Seller,
    Site,
    SiteNote,
    SiteStatus,
    Upload,
    UploadItem,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import catalog, review, service

pytestmark = pytest.mark.django_db

HEADERS = list(catalog.REQUIRED)


def _row(domain: str, price: str = "100.00") -> dict[str, str]:
    return {
        "Domain": f"https://www.{domain}",
        "Collaborator URL": f"https://collaborator.pro/{domain}",
        "Category": "Internet",
        "Website languages": "English",
        "Website type": "Personal blog",
        "Link type article": "dofollow",
        "Advertising mark article": "No",
        "Number of links article": "1",
        "Publishing price article, EUR": price,
        "DR": "40",
        "Organic traffic": "5000",
        "Keywords": "200",
    }


def _csv(domains: list[str]) -> bytes:
    lines = [";".join(f'"{h}"' for h in HEADERS)]
    for domain in domains:
        row = _row(domain)
        lines.append(";".join(f'"{row.get(h, "")}"' for h in HEADERS))
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


@pytest.fixture(autouse=True)
def setup(settings: Any, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"
    Product.objects.create(name="Convertio", domain="convertio.co")
    Product.objects.create(name="Clideo", domain="clideo.com")


def _load(domains: list[str], day: dt.date = dt.date(2026, 10, 1), name: str = "c.csv") -> Upload:
    file = SimpleUploadedFile(name, _csv(domains))
    upload = service.create_upload(
        file, kind=UploadKind.COLLABORATOR_CATALOG, seller=Seller.collaborator(), prices_date=day
    ).upload
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk, "new", ["prices", "metrics", "card"], {})
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


def _decide(client: Client, upload: Upload, action: str, items: list[int]) -> dict[str, Any]:
    response = client.post(
        reverse("admin:sites_upload_decide", args=[upload.pk]),
        data=json.dumps({"action": action, "items": items}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()  # type: ignore[no-any-return]


def _undo(client: Client, upload: Upload, entries: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(
        reverse("admin:sites_upload_undo", args=[upload.pk]),
        data=json.dumps({"undo": entries}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


class TestDelete:
    def test_delete_hides_and_undo_returns(self, admin_client: Client) -> None:
        upload = _load(["junk.com", "good.com"])
        item = UploadItem.objects.get(upload=upload, site__domain="junk.com")
        result = _decide(admin_client, upload, "delete", [item.pk])
        assert result["rows"][str(item.pk)]["state"] == "removed"
        assert result["problems"] == []
        site = Site.all_objects.get(domain="junk.com")
        assert site.is_deleted
        assert not Site.objects.filter(domain="junk.com").exists()
        assert [t.total for t in review.tabs(upload) if t.key == ReviewGroup.NEW] == [1]
        assert SiteNote.objects.filter(site=site, body__startswith="Удалена из базы").exists()

        result = _undo(admin_client, upload, result["undo"])
        assert result["rows"][str(item.pk)]["state"] != "removed"
        site.refresh_from_db()
        assert not site.is_deleted

    def test_site_with_history_is_not_deleted(self, admin_client: Client) -> None:
        upload = _load(["placed.com"])
        item = UploadItem.objects.get(upload=upload)
        Placement.objects.create(
            site_id=item.site_id,
            product=Product.objects.get(domain="convertio.co"),
            status=PlacementStatus.PUBLISHED,
        )
        result = _decide(admin_client, upload, "delete", [item.pk])
        assert result["undo"] == []
        assert "размещения" in result["problems"][0]
        assert not Site.all_objects.get(pk=item.site_id).is_deleted

    def test_deleted_site_comes_back_as_new_in_next_upload(self, admin_client: Client) -> None:
        first = _load(["junk.com"])
        item = UploadItem.objects.get(upload=first)
        _decide(admin_client, first, "delete", [item.pk])
        second = _load(["junk.com"], day=dt.date(2026, 10, 11), name="c2.csv")
        site = Site.all_objects.get(domain="junk.com")
        assert not site.is_deleted
        assert Site.all_objects.filter(domain="junk.com").count() == 1
        assert (second.result or {})["runs"][0]["counts"]["sites_restored"] == 1


class TestBlock:
    def test_block_blacklists_for_all_products_and_skips_next_uploads(
        self, admin_client: Client
    ) -> None:
        upload = _load(["spam.com", "good.com"])
        item = UploadItem.objects.get(upload=upload, site__domain="spam.com")
        result = _decide(admin_client, upload, "block", [item.pk])
        assert result["rows"][str(item.pk)]["state"] == "removed"
        statuses = set(
            ProductSite.objects.filter(site_id=item.site_id).values_list("status", flat=True)
        )
        assert statuses == {SiteStatus.BLACKLISTED}
        # Из разбора пропала, из базы — нет.
        assert [t.total for t in review.tabs(upload) if t.key == ReviewGroup.NEW] == [1]
        assert Site.objects.filter(domain="spam.com").exists()

        second = _load(["spam.com", "good.com"], day=dt.date(2026, 10, 11), name="c2.csv")
        assert second.summary is not None
        assert second.summary["blocked_total"] == 1
        assert not UploadItem.objects.filter(upload=second, site_id=item.site_id).exists()

    def test_undo_block_restores_statuses(self, admin_client: Client) -> None:
        upload = _load(["spam.com"])
        item = UploadItem.objects.get(upload=upload)
        convertio = ProductSite.objects.get(site_id=item.site_id, product__domain="convertio.co")
        convertio.status = SiteStatus.APPROVED
        convertio.save()
        result = _decide(admin_client, upload, "block", [item.pk])
        _undo(admin_client, upload, result["undo"])
        convertio.refresh_from_db()
        assert convertio.status == SiteStatus.APPROVED
        clideo = ProductSite.objects.get(site_id=item.site_id, product__domain="clideo.com")
        assert clideo.status == SiteStatus.NEW

    def test_blacklisted_for_one_product_is_not_blocked(self) -> None:
        upload = _load(["half.com"])
        item = UploadItem.objects.get(upload=upload)
        ProductSite.objects.filter(site_id=item.site_id, product__domain="convertio.co").update(
            status=SiteStatus.BLACKLISTED
        )
        assert [t.total for t in review.tabs(upload) if t.key == ReviewGroup.NEW] == [1]


class TestAjaxAnswers:
    def test_run_buttons_answer_json(self, admin_client: Client) -> None:
        file = SimpleUploadedFile("c.csv", _csv(["a.com"]))
        upload = service.create_upload(
            file,
            kind=UploadKind.COLLABORATOR_CATALOG,
            seller=Seller.collaborator(),
            prices_date=dt.date(2026, 10, 1),
        ).upload
        service.start(upload, UploadStatus.CHECKING)
        upload_check.delay(upload.pk)
        url = reverse("admin:sites_upload_write", args=[upload.pk])
        response = admin_client.post(url, {"action": "known"}, HTTP_X_SEO_AJAX="1")
        assert response.status_code == 400
        assert "Отметьте" in response.json()["error"]
        response = admin_client.post(
            url, {"action": "new", "parts": ["prices"], "filters": "{}"}, HTTP_X_SEO_AJAX="1"
        )
        assert response.json() == {
            "state_url": reverse("admin:sites_upload_state", args=[upload.pk])
        }
        upload.refresh_from_db()
        assert upload.status == UploadStatus.DONE
        response = admin_client.post(
            reverse("admin:sites_upload_recheck", args=[upload.pk]), HTTP_X_SEO_AJAX="1"
        )
        assert "state_url" in response.json()

    def test_steps_load_shared_navigation(self, admin_client: Client) -> None:
        """Шаги загрузки подгружаются общей подгрузкой админки (E9-09)."""
        upload = _load(["a.com"])
        page = admin_client.get(reverse("admin:sites_upload_review", args=[upload.pk]))
        content = page.content.decode()
        assert "seo/soft-nav.js" in content
        assert "seo/uploads.js" in content


def test_recheck_after_write_keeps_review_open(admin_client: Client) -> None:
    upload = _load(["a.com"])
    admin_client.post(reverse("admin:sites_upload_recheck", args=[upload.pk]), HTTP_X_SEO_AJAX="1")
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE
    response = admin_client.get(reverse("admin:sites_upload_review", args=[upload.pk]))
    assert response.status_code == 200
