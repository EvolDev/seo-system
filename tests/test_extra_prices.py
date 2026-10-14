"""Цены написания и анонса: карандаш в карточке, действие в «Площадках», загрузка прайса.

Продавцы обычно называют их отдельно от прайса (пользователь, 14.10.2026), поэтому
их вписывают руками: у одной площадки, у отмеченных строк или на весь файл (ADR-071).
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.sites import offers
from apps.sites.models import (
    ExchangeRate,
    PlacementType,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteNote,
    SitePrice,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import service
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

PRICE_DATE = dt.date(2026, 10, 1)


@pytest.fixture
def seller() -> Seller:
    return Seller.objects.create(name="Katerina", currency="USD")


def _price(
    domain: str,
    seller: Seller,
    *,
    currency: str = "EUR",
    service: PlacementType = PlacementType.GUEST_POST,
    writing: int | None = None,
    day: dt.date = dt.date(2026, 9, 20),
) -> SitePrice:
    site = Site.objects.filter(domain=domain).first() or Site.objects.create(domain=domain)
    price = SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_type=service,
        placement_cents=20000,
        currency=currency,
        writing_cents=writing,
        checked_at=start_of_day(day),
    )
    offers.set_working_price(price)
    return price


class TestEdit:
    def test_in_place_with_note(self, seller: Seller) -> None:
        price = _price("a.com", seller)
        assert offers.set_extra_price(price, "writing", 3000)
        price.refresh_from_db()
        assert price.writing_cents == 3000
        assert SitePrice.objects.filter(site=price.site).count() == 1
        # Заметки — только о ценах услуг; рабочую цену `_price` ставит своей заметкой.
        before = SiteNote.objects.filter(site=price.site).exclude(body__startswith="Написание")
        skip = list(before.values_list("pk", flat=True))
        note = SiteNote.objects.exclude(pk__in=skip).get(site=price.site)
        assert note.body.startswith("Написание · Katerina")
        assert note.body.endswith("не указано → €30")
        # Та же сумма — без изменений и без второй заметки; пусто — «не указано».
        assert not offers.set_extra_price(price, "writing", 3000)
        assert offers.set_extra_price(price, "announce", 0)
        assert offers.set_extra_price(price, "writing", None)
        fresh = SitePrice.objects.get(pk=price.pk)
        assert (fresh.writing_cents, fresh.announce_cents) == (None, 0)
        assert SiteNote.objects.filter(site=price.site).exclude(pk__in=skip).count() == 3

    def test_newest_snapshot_too(self, seller: Seller) -> None:
        working = _price("b.com", seller)
        # Новый прайс пришёл, а рабочая осталась прежней: заявка в работе.
        newer = SitePrice.objects.create(
            site=working.site,
            seller=seller,
            placement_cents=25000,
            checked_at=start_of_day(PRICE_DATE),
        )
        offers.set_extra_price(working, "writing", 4000)
        newer.refresh_from_db()
        assert newer.writing_cents == 4000

    def test_link_insertion_refused(self, seller: Seller) -> None:
        price = _price("c.com", seller, service=PlacementType.LINK_INSERTION)
        with pytest.raises(ValueError, match="вставки"):
            offers.set_extra_price(price, "writing", 3000)

    def test_card(self, admin_client: Client, seller: Seller) -> None:
        price = _price("d.com", seller)
        url = reverse("admin:sites_site_card_extra_price", args=[price.site_id])
        card = admin_client.get(reverse("admin:sites_site_card", args=[price.site_id]))
        assert "не указано — пишем сами" in card.content.decode()
        response = admin_client.post(
            url, {"offer": price.pk, "kind": "writing", "amount": "35,5"}, HTTP_X_SEO_PARTIAL="1"
        )
        assert response.status_code == 200
        price.refresh_from_db()
        assert price.writing_cents == 3550
        wrong = admin_client.post(
            url, {"offer": price.pk, "kind": "announce", "amount": "abc"}, HTTP_X_SEO_PARTIAL="1"
        )
        html = wrong.content.decode()
        assert "Введите число." in html
        # Форма с ошибкой осталась открытой у анонса этого предложения.
        assert 'name="kind" value="announce"' in html
        price.refresh_from_db()
        assert price.announce_cents is None


class TestAction:
    def test_writing_for_selected(self, admin_client: Client, seller: Seller) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.1"))
        eur = _price("eur.com", seller)
        usd = _price("usd.com", seller, currency="USD")
        insertion = _price("li.com", seller, service=PlacementType.LINK_INSERTION)
        bare = Site.objects.create(domain="bare.com")
        rows = ProductSite.objects.filter(site__in=[eur.site, usd.site, insertion.site, bare])
        response = admin_client.post(
            reverse("admin:sites_productsitelatest_changelist"),
            {
                "action": "set_writing_action",
                "_selected_action": [str(pk) for pk in rows.values_list("pk", flat=True)],
                "writing": "30",
            },
            follow=True,
        )
        text = " ".join(str(m) for m in response.context["messages"])
        assert "поставлена: 2" in text
        assert "нет рабочей цены — 1" in text and "вставка ссылки" in text
        assert "пересчитано по курсу ЕЦБ" in text
        eur.refresh_from_db()
        usd.refresh_from_db()
        insertion.refresh_from_db()
        assert eur.writing_cents == 3000
        assert usd.writing_cents == 3300  # €30 × 1.1
        assert insertion.writing_cents is None

    def test_empty_amount_warns(self, admin_client: Client, seller: Seller) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        price = _price("e.com", seller)
        row = ProductSite.objects.get(site=price.site)
        response = admin_client.post(
            reverse("admin:sites_productsitelatest_changelist"),
            {"action": "set_writing_action", "_selected_action": [str(row.pk)], "writing": ""},
            follow=True,
        )
        assert "Впишите цену написания" in " ".join(str(m) for m in response.context["messages"])
        price.refresh_from_db()
        assert price.writing_cents is None


class TestUpload:
    @pytest.fixture(autouse=True)
    def uploads_dir(self, settings: object, tmp_path: Path) -> None:
        settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]

    def upload(self, seller: Seller, content: str) -> Upload:
        file = SimpleUploadedFile("price.csv", content.encode("utf-8"))
        upload = service.create_upload(
            file, kind=UploadKind.PRICE_LIST, seller=seller, prices_date=PRICE_DATE
        ).upload
        assert upload is not None
        if service.needs_questions(upload):
            assert service.confirm_mapping(upload, upload.mapping or {}, "EUR") == []
        service.start(upload, UploadStatus.CHECKING)
        upload_check.delay(upload.pk)
        upload.refresh_from_db()
        return upload

    def write(self, admin_client: Client, upload: Upload, **fields: str) -> Upload:
        response = admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]),
            {"action": "all", **fields},
            HTTP_X_SEO_AJAX="1",
        )
        assert response.status_code in (200, 302)
        upload.refresh_from_db()
        if upload.status == UploadStatus.CHECKED:
            # Без заголовка подгрузки ответ — переход: запустим запись сами.
            service.start(upload, UploadStatus.WRITING)
            upload_write.delay(upload.pk)
            upload.refresh_from_db()
        return upload

    def test_summary_counts_and_defaults(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="Morgan", currency="EUR")
        known = _price("known.com", seller, writing=2000)
        upload = self.upload(
            seller,
            "Website,GP Price,LI Price,Writing\n"
            "new.com,200,,\n"
            "known.com,210,,\n"
            "own.com,150,,45\n"
            "li.com,,90,\n",
        )
        summary = upload.summary or {}
        assert (summary["guest_posts"], summary["no_writing"], summary["no_announce"]) == (3, 2, 3)
        page = admin_client.get(reverse("admin:sites_upload_summary", args=[upload.pk]))
        assert "Цены, которых нет в файле" in page.content.decode()
        upload = self.write(admin_client, upload, writing="30", announce="")
        assert (upload.writing_cents, upload.announce_cents) == (3000, None)

        def newest(domain: str, service: PlacementType = PlacementType.GUEST_POST) -> SitePrice:
            return SitePrice.objects.filter(
                site__domain=domain, seller=seller, placement_type=service
            ).latest("checked_at", "pk")

        assert newest("new.com").writing_cents == 3000  # из поля сводки
        assert newest("own.com").writing_cents == 4500  # своя цена файла главнее
        assert newest("known.com").writing_cents == 3000  # поле сводки главнее прежней
        assert newest("li.com", PlacementType.LINK_INSERTION).writing_cents is None
        assert newest("new.com").announce_cents is None
        known.refresh_from_db()
        assert known.writing_cents == 2000  # старый снимок не трогаем

    def test_previous_price_survives(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="Zain", currency="EUR")
        known = _price("kept.com", seller)
        offers.set_extra_price(known, "writing", 2500)
        offers.set_extra_price(known, "announce", 1000)
        upload = self.write(admin_client, self.upload(seller, "Website,GP Price\nkept.com,220\n"))
        assert upload.status == UploadStatus.DONE
        fresh = SitePrice.objects.filter(site=known.site, seller=seller).latest("checked_at", "pk")
        assert fresh.pk != known.pk and fresh.placement_cents == 22000
        assert (fresh.writing_cents, fresh.announce_cents) == (2500, 1000)

    def test_wrong_amount(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="Wrong", currency="EUR")
        upload = self.upload(seller, "Website,GP Price\nx.com,100\n")
        response = admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]),
            {"action": "all", "writing": "тридцать"},
            HTTP_X_SEO_AJAX="1",
        )
        upload.refresh_from_db()
        assert upload.status == UploadStatus.CHECKED
        assert upload.writing_cents is None
        assert response.status_code == 400
        assert "Цена написания" in response.json()["error"]
