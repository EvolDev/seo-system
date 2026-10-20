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
from apps.placements.models import Placement, PlacementLink, PlacementStatus
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

    def test_catalog_upload_restores_deleted_collaborator(self, admin_client: Client) -> None:
        """Каталог удалён — загрузка заводит его заново, а не падает с 500.

        Удалить можно любую запись (ADR-060), в том числе посевного продавца
        из миграции `0007`. До этого форма звала `Seller.objects.get` и
        роняла шаг «Файл» на чистой базе (07.10.2026).
        """
        Seller.objects.filter(is_collaborator=True).delete()
        response = _post_file(admin_client, CSV, kind="collaborator_catalog")
        assert response.status_code == 302
        seller = Seller.objects.get(is_collaborator=True)
        # Флаг важнее имени: с выключенным замеры каталога станут «со слов
        # продавца» по всей базе (ADR-043).
        fields = (seller.name, seller.currency, seller.metrics_trusted)
        assert fields == ("Collaborator", "EUR", True)
        assert Upload.objects.get().seller == seller

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

    def test_review_copies_all_domains_and_pages_by_choice(
        self, admin_client: Client, linkhub: Seller, known: Site
    ) -> None:
        """«Скопировать все домены» — вся вкладка; «На странице» — 50…500."""
        upload = _uploaded(admin_client, linkhub)
        url = reverse("admin:sites_upload_review", args=[upload.pk])
        item = UploadItem.objects.filter(upload=upload).first()
        assert item is not None
        tab = item.review_group
        answer = admin_client.get(url, {"tab": tab, "all": "1", "domains": "1"}).json()
        expected = list(
            dict.fromkeys(
                UploadItem.objects.filter(upload=upload, review_group=tab)
                .order_by("line", "pk")
                .values_list("site__domain", flat=True)
            )
        )
        assert answer == {"text": "\n".join(expected), "count": len(expected)}
        page = admin_client.get(url, {"tab": tab, "all": "1", "per_page": "250"}).content.decode()
        assert "На странице:" in page and "<b>250</b>" in page
        assert "per_page=250" in page  # вкладки и порядок помнят размер
        assert f'data-domain="{item.site.domain}"' in page
        assert "Скопировать все домены" in page

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


class TestPlacementsKeepAnchors:
    """Прайс продавца не трогает его размещения «В работе» и «Заявка отправлена».

    Проверка по просьбе пользователя (20.10.2026): у таких размещений не было
    анкоров. Запись прайса, решения разбора, их отмена и отмена всей загрузки
    оставляют размещения и их ссылки как были.
    """

    @pytest.fixture
    def placements(self, known: Site, linkhub: Seller) -> list[Placement]:
        product = Product.objects.get(name="Convertio")
        result = []
        for status in (PlacementStatus.IN_WORK, PlacementStatus.ORDERED):
            placement = Placement.objects.create(
                site=known, product=product, seller=linkhub, status=status
            )
            placement.links.create(
                anchor="mp4 to mp3", target_url="https://convertio.co/mp4-mp3/", link_index=1
            )
            placement.links.create(
                anchor="Convertio", target_url="https://convertio.co/", link_index=2
            )
            result.append(placement)
        return result

    @staticmethod
    def _state() -> tuple[list[Any], list[Any]]:
        links = PlacementLink.objects.order_by("pk").values_list(
            "pk", "placement_id", "anchor", "target_url", "link_index", "keyword_id"
        )
        rows = Placement.objects.order_by("pk").values_list("pk", "status", "seller_id", "price_id")
        return list(links), list(rows)

    def _decide(self, client: Client, upload: Upload, action: str, item: UploadItem) -> Any:
        return client.post(
            reverse("admin:sites_upload_decide", args=[upload.pk]),
            data=json.dumps({"action": action, "items": [item.pk]}),
            content_type="application/json",
        )

    def test_write_review_undo_and_cancel(
        self, admin_client: Client, linkhub: Seller, known: Site, placements: list[Placement]
    ) -> None:
        before = self._state()
        assert len(before[0]) == 4

        upload = _uploaded(admin_client, linkhub)
        assert upload.status == UploadStatus.DONE
        assert self._state() == before

        item = UploadItem.objects.get(upload=upload, site=known)
        fixed = self._decide(admin_client, upload, "fix", item)
        assert fixed.status_code == 200
        assert self._state() == before
        admin_client.post(
            reverse("admin:sites_upload_undo", args=[upload.pk]),
            data=json.dumps({"undo": fixed.json()["undo"]}),
            content_type="application/json",
        )
        assert self._state() == before

        # Площадку с размещениями из базы не удалить — только заблокировать.
        self._decide(admin_client, upload, "delete", item)
        assert self._state() == before

        admin_client.post(reverse("admin:sites_upload_delete", args=[upload.pk]), {"post": "yes"})
        assert not Upload.objects.filter(pk=upload.pk).exists()
        assert self._state() == before

    def test_anchors_after_upload_survive_cancel(
        self, admin_client: Client, linkhub: Seller, known: Site, placements: list[Placement]
    ) -> None:
        """Анкоры правили на следующий день после прайса, потом прайс отменили.

        Журнал загрузки помнит только то, что записала сама загрузка, поэтому
        отмена не возвращает правки людей. Размещение, заведённое через цену из
        этого прайса, остаётся с анкорами — пропадает только ссылка на цену.
        """
        upload = _uploaded(admin_client, linkhub)
        item = UploadItem.objects.get(upload=upload, site=known)
        ordered = placements[1]
        ordered.links.filter(link_index=1).update(anchor="mov to mp4")
        ordered.links.create(anchor="convert", target_url="https://convertio.co/", link_index=3)
        through_new_price = Placement.objects.create(
            site=known,
            product=ordered.product,
            seller=linkhub,
            status=PlacementStatus.ORDERED,
            price_id=item.price_id,
        )
        through_new_price.links.create(
            anchor="mp4 to mp3", target_url="https://convertio.co/mp4-mp3/", link_index=1
        )
        before_links = self._state()[0]

        url = reverse("admin:sites_upload_delete", args=[upload.pk])
        page = admin_client.get(url).content.decode()
        # Окно отмены не обещает удалить размещения: площадка была до прайса.
        assert "Отменить загрузку" in page
        assert "Удалится — размещения" not in page
        admin_client.post(url, {"post": "yes"})

        assert not Upload.objects.filter(pk=upload.pk).exists()
        assert self._state()[0] == before_links
        through_new_price.refresh_from_db()
        # Цена из отменённого прайса ушла — у размещения пусто, как у старых без цены.
        assert through_new_price.price_id is None
