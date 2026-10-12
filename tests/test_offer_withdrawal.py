"""Снятие с продажи по полному каталогу Collaborator (ADR-068).

Через сервис и задачи очереди (в тестах они выполняются сразу), экраны — через
клиент админки. Каталог старый — 01.10, новый — 20.10.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest
from django.contrib.admin.views.main import ChangeList
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements.models import Placement, PlacementStatus
from apps.sites.importing.report import Section
from apps.sites.importing.run import ImportOptions, run_import
from apps.sites.models import (
    MetricSource,
    Product,
    ProductSiteLatest,
    Seller,
    Site,
    SiteLatest,
    SiteNote,
    SiteOffer,
    SitePrice,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import catalog, service, undo, withdraw
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

OLD = dt.date(2026, 10, 1)
NEW = dt.date(2026, 10, 20)
FILLERS = ["fresh1.com", "fresh2.com", "fresh3.com", "fresh4.com"]
SITES_URL = reverse("admin:sites_productsitelatest_changelist")


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def collaborator() -> Seller:
    return Seller.collaborator()


@pytest.fixture
def linkhub() -> Seller:
    return Seller.objects.create(name="LinkHub Media")


def _offer(
    site: Site,
    seller: Seller,
    cents: int,
    day: dt.date = OLD,
    *,
    working: bool = False,
    reviewed: bool = True,
    **fields: object,
) -> SitePrice:
    price = SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_cents=cents,
        currency="EUR",
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(day),
        reviewed_at=timezone.now() if reviewed else None,
        **fields,
    )
    if working:
        site.price = price
        site.save(update_fields=["price"])
    return price


@pytest.fixture
def base(convertio: Product, collaborator: Seller, linkhub: Seller) -> dict[str, Site]:
    """Площадки базы. В новом каталоге из них есть только kept.com."""
    sites = {
        name: Site.objects.create(domain=f"{name}.com")
        for name in ("kept", "gone", "shared", "ordered", "side", "newer", "deleted")
    }
    _offer(sites["kept"], collaborator, 10000, working=True)
    _offer(
        sites["gone"],
        collaborator,
        12000,
        working=True,
        link_type="dofollow",
        link_type_manual=True,
    )
    _offer(sites["shared"], collaborator, 13000, working=True)
    _offer(sites["shared"], linkhub, 15000)
    _offer(sites["ordered"], collaborator, 14000, working=True)
    Placement.objects.create(
        site=sites["ordered"], product=convertio, status=PlacementStatus.ORDERED
    )
    _offer(sites["side"], linkhub, 11000, working=True)
    _offer(sites["side"], collaborator, 9000, reviewed=False)
    _offer(sites["newer"], collaborator, 16000, NEW + dt.timedelta(days=1), working=True)
    _offer(sites["deleted"], collaborator, 17000, working=True)
    Site.all_objects.filter(pk=sites["deleted"].pk).update(is_deleted=True)
    return sites


def _row(domain: str, price: str = "120.00") -> dict[str, str]:
    return {
        catalog.DOMAIN: f"https://www.{domain}",
        catalog.COLLABORATOR_URL: f"https://collaborator.pro/{domain}",
        catalog.CATEGORY: "Internet, Marketing",
        catalog.LANGUAGES: "English",
        catalog.SITE_TYPE: "Personal blog",
        catalog.LINK_TYPE: "dofollow",
        catalog.MARKS_AS_AD: "No",
        catalog.LINKS_ALLOWED: "2",
        catalog.PRICE: price,
        catalog.ANNOUNCE: "free",
        catalog.DR: "50",
        catalog.TRAFFIC: "12000",
        catalog.KEYWORDS: "300",
    }


def _catalog(
    seller: Seller, rows: list[dict[str, str]], day: dt.date = NEW, name: str = "collaborator.csv"
) -> Upload:
    """Каталог загружен и проверен — как после «Проверить» на экране."""
    lines = [";".join(f'"{header}"' for header in catalog.REQUIRED)]
    lines += [";".join(f'"{row.get(header, "")}"' for header in catalog.REQUIRED) for row in rows]
    file = SimpleUploadedFile(name, ("\n".join(lines) + "\n").encode("utf-8"))
    upload = service.create_upload(
        file, kind=UploadKind.COLLABORATOR_CATALOG, seller=seller, prices_date=day
    ).upload
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.CHECKED, upload.error
    return upload


def _new_catalog(seller: Seller, *domains: str, day: dt.date = NEW) -> Upload:
    return _catalog(seller, [_row(domain) for domain in [*domains, *FILLERS]], day)


def _press(upload: Upload, action: str) -> Upload:
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk, action, ["prices", "metrics", "card"])
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


def _last(site: Site, seller: Seller) -> SitePrice:
    return SitePrice.objects.filter(site=site, seller=seller).latest("checked_at", "pk")


def _domains(client: Client, **params: str) -> set[str]:
    response = client.get(SITES_URL, {"list": "all", **params})
    assert response.status_code == 200
    changelist: ChangeList = response.context["cl"]
    return {row.domain for row in changelist.result_list}


class TestSummary:
    def test_absent_sites_before_writing(self, base: dict[str, Site], collaborator: Seller) -> None:
        upload = _new_catalog(collaborator, "kept.com")
        absent = (upload.summary or {})["absent"]
        assert {key: absent[key] for key in ("total", "working", "others", "stop", "frozen")} == {
            "total": 3,  # gone, shared, side; ordered — с заявкой, newer — свежее файла
            "working": 2,  # gone, shared
            "others": 2,  # shared, side
            "stop": 1,  # gone — больше никто не продаёт
            "frozen": 1,  # ordered
        }
        assert absent["looks_partial"] is False
        notes = {row["domain"]: row["note"] for row in absent["list"]}
        assert notes == {
            "gone.com": "рабочая цена снимется, больше никто не продаёт",
            "ordered.com": "заявка в работе — не трогаем",
            "shared.com": "рабочая цена снимется, есть другие продавцы",
            "side.com": "есть другие продавцы",
        }

    def test_row_without_price_still_counts_as_listed(
        self, base: dict[str, Site], collaborator: Seller
    ) -> None:
        rows = [_row(d) for d in ["kept.com", *FILLERS]] + [_row("gone.com", price="")]
        upload = _catalog(collaborator, rows)
        listed = {row["domain"] for row in (upload.summary or {})["absent"]["list"]}
        assert "gone.com" not in listed

    def test_filtered_export_looks_partial(
        self, base: dict[str, Site], collaborator: Seller
    ) -> None:
        upload = _catalog(collaborator, [_row("kept.com")])
        assert (upload.summary or {})["absent"]["looks_partial"] is True


class TestWithdraw:
    def test_withdraws_absent_offers(
        self, base: dict[str, Site], collaborator: Seller, linkhub: Seller
    ) -> None:
        upload = _press(_new_catalog(collaborator, "kept.com"), "withdraw")

        gone = Site.objects.get(domain="gone.com")
        snapshot = _last(gone, collaborator)
        assert snapshot.withdrawn
        assert snapshot.placement_cents is None
        assert snapshot.checked_at == start_of_day(NEW)
        assert snapshot.reviewed_at is not None
        # Ручное уточнение типа ссылки переживёт возвращение площадки.
        assert (snapshot.link_type, snapshot.link_type_manual) == ("dofollow", True)
        assert gone.price_id is None
        note = SiteNote.objects.get(site=gone)
        assert note.body == (
            "Цена: Collaborator · публикация €120 → снята с продажи "
            "(каталог Collaborator от 20.10.2026)"
        )
        assert note.source == "collaborator.csv"
        assert not SiteOffer.objects.filter(site=gone).exists()
        assert SiteLatest.objects.get(site=gone).withdrawn_at == start_of_day(NEW)
        # «Пишем мы» без рабочей цены — пусто: решать не по чему.
        assert ProductSiteLatest.objects.get(site_id=gone.pk).we_write is None

        shared = Site.objects.get(domain="shared.com")
        assert shared.price_id is None
        offered = SiteOffer.objects.filter(site=shared).values_list("seller_id", flat=True)
        assert set(offered) == {linkhub.pk}
        assert SiteLatest.objects.get(site=shared).withdrawn_at is None

        side = Site.objects.get(domain="side.com")
        assert _last(side, collaborator).withdrawn
        assert side.price is not None and side.price.seller_id == linkhub.pk
        assert not SiteNote.objects.filter(site=side).exists()
        assert not SitePrice.objects.filter(site=side, reviewed_at__isnull=True).exists()

        for name in ("ordered", "newer", "kept"):
            site = Site.all_objects.get(domain=f"{name}.com")
            assert not SitePrice.objects.filter(site=site, withdrawn=True).exists(), name
            assert site.price_id is not None
        deleted = Site.all_objects.get(domain="deleted.com")
        assert not SitePrice.objects.filter(site=deleted, withdrawn=True).exists()

        run = (upload.result or {})["runs"][-1]
        assert run["action"] == "withdraw"
        assert run["counts"] == {
            "sites_withdrawn": 3,
            "offers_withdrawn": 3,
            "working_cleared": 2,
            "skipped_frozen": 1,
        }
        absent = (upload.summary or {})["absent"]
        assert (absent["total"], absent["frozen"]) == (0, 1)

    def test_second_press_changes_nothing(
        self, base: dict[str, Site], collaborator: Seller
    ) -> None:
        upload = _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        rows = SitePrice.objects.count()
        _press(upload, "withdraw")
        assert SitePrice.objects.count() == rows
        assert (upload.result or {})["runs"][-1]["counts"]["sites_withdrawn"] == 0

    def test_filtered_export_is_refused(self, base: dict[str, Site], collaborator: Seller) -> None:
        upload = _catalog(collaborator, [_row("kept.com")])
        result = withdraw.run(upload, {"kept.com"})
        assert result["counts"]["refused_partial"] == 1
        assert not SitePrice.objects.filter(withdrawn=True).exists()


class TestReturn:
    def test_next_catalog_brings_it_back(self, base: dict[str, Site], collaborator: Seller) -> None:
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        later = _new_catalog(collaborator, "kept.com", "gone.com", day=NEW + dt.timedelta(days=10))
        _press(later, "known")
        gone = Site.objects.get(domain="gone.com")
        assert gone.price is not None
        assert (gone.price.placement_cents, gone.price.withdrawn) == (12000, False)
        assert SiteOffer.objects.filter(site=gone).exists()
        assert SiteLatest.objects.get(site=gone).withdrawn_at is None
        # Ручной тип ссылки перешёл на вернувшуюся цену (ADR-066).
        assert (gone.price.link_type, gone.price.link_type_manual) == ("dofollow", True)

    def test_older_catalog_does_not_bring_it_back(
        self, base: dict[str, Site], collaborator: Seller
    ) -> None:
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        older = _new_catalog(collaborator, "kept.com", "gone.com", day=dt.date(2026, 10, 10))
        _press(older, "known")
        gone = Site.objects.get(domain="gone.com")
        assert gone.price_id is None
        assert not SiteOffer.objects.filter(site=gone).exists()
        assert SiteLatest.objects.get(site=gone).withdrawn_at == start_of_day(NEW)

    def test_undo_restores_everything(self, base: dict[str, Site], collaborator: Seller) -> None:
        working = Site.objects.get(domain="gone.com").price_id
        upload = _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        undo.undo(upload)
        gone = Site.objects.get(domain="gone.com")
        assert gone.price_id == working
        assert not SitePrice.objects.filter(withdrawn=True).exists()
        assert not SiteNote.objects.filter(site=gone).exists()
        assert SiteLatest.objects.get(site=gone).withdrawn_at is None

    def test_workbook_price_does_not_bring_it_back(
        self,
        base: dict[str, Site],
        collaborator: Seller,
        make_workbook: Callable[..., Path],
        tmp_path: Path,
    ) -> None:
        Product.objects.create(name="Clideo", domain="clideo.com")
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        book = make_workbook(base=[("gone.com", {})])
        options = ImportOptions(
            path=book, as_of=dt.date(2026, 10, 25), list_name="Октябрь 2026", dry_run=False
        )
        report = run_import(options, tmp_path / "reports").report
        gone = Site.objects.get(domain="gone.com")
        assert gone.price_id is None
        assert _last(gone, collaborator).withdrawn
        assert report.issues[Section.WITHDRAWN] == ["gone.com"]


class TestScreens:
    def test_sites_list_hides_withdrawn_by_default(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        assert "gone.com" not in _domains(admin_client)
        assert _domains(admin_client, sale="only") == {"gone.com"}
        assert "gone.com" in _domains(admin_client, sale="all")
        page = admin_client.get(SITES_URL, {"list": "all", "sale": "only"}).content.decode()
        assert "не продаётся" in page

    def test_seller_filter_counts_only_current_offers(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        found = _domains(admin_client, sale="all", seller=str(collaborator.pk))
        assert found == {"kept.com", "ordered.com", "newer.com"}

    def test_card_says_not_for_sale(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        _press(_new_catalog(collaborator, "kept.com"), "withdraw")
        gone = Site.objects.get(domain="gone.com")
        page = admin_client.get(reverse("admin:sites_site_card", args=[gone.pk])).content.decode()
        assert "Не продаётся: у Collaborator снята с продажи 20.10.2026" in page
        assert "снята с продажи</span>" in page  # строка истории цен

    def test_catalog_page_shows_the_block(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        upload = _new_catalog(collaborator, "kept.com")
        page = admin_client.get(
            reverse("admin:sites_upload_summary", args=[upload.pk])
        ).content.decode()
        assert "Нет в этом файле — 3" in page
        assert 'name="full" value="1" required' in page

    def test_button_needs_full_catalog_mark(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        upload = _new_catalog(collaborator, "kept.com")
        url = reverse("admin:sites_upload_write", args=[upload.pk])
        ajax = {"X-Seo-Ajax": "1"}
        refused = admin_client.post(url, {"action": "withdraw"}, headers=ajax)
        assert refused.status_code == 400
        assert "полный каталог" in refused.json()["error"]
        assert not SitePrice.objects.filter(withdrawn=True).exists()
        started = admin_client.post(url, {"action": "withdraw", "full": "1"}, headers=ajax)
        assert started.status_code == 200, started.content
        assert SitePrice.objects.filter(withdrawn=True).count() == 3

    def test_filtered_export_button_is_refused(
        self, admin_client: Client, base: dict[str, Site], collaborator: Seller
    ) -> None:
        upload = _catalog(collaborator, [_row("kept.com")])
        url = reverse("admin:sites_upload_write", args=[upload.pk])
        answer = admin_client.post(
            url, {"action": "withdraw", "full": "1"}, headers={"X-Seo-Ajax": "1"}
        )
        assert answer.status_code == 400
        assert "выгрузку с фильтром" in answer.json()["error"]
        assert not SitePrice.objects.filter(withdrawn=True).exists()

    def test_price_list_has_no_withdraw(
        self, admin_client: Client, base: dict[str, Site], linkhub: Seller
    ) -> None:
        file = SimpleUploadedFile("hub.csv", b"Website,GP Price\nkept.com,$100\n")
        upload = service.create_upload(
            file, kind=UploadKind.PRICE_LIST, seller=linkhub, prices_date=NEW
        ).upload
        Upload.objects.filter(pk=upload.pk).update(status=UploadStatus.CHECKED)
        answer = admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]),
            {"action": "withdraw", "full": "1"},
            headers={"X-Seo-Ajax": "1"},
        )
        assert answer.status_code == 400
        assert not SitePrice.objects.filter(withdrawn=True).exists()
