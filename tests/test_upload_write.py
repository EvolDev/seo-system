"""Загрузка целиком: файл → разметка → сводка → запись → разбор (E1-08, ADR-044).

Через сервис и задачи очереди (в тестах они выполняются сразу). Правило
вкладок по отдельности — `test_upload_plan.py`.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.observability.models import TaskRun, TaskStatus
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    ExchangeRate,
    MetricSource,
    PlacementType,
    Product,
    ProductSite,
    ReviewGroup,
    Seller,
    Site,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
    Upload,
    UploadItem,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import catalog, service
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

PRICE_DATE = dt.date(2026, 10, 1)
HEADER = "Website,GP Price,LI Price,DR,Комментарий\n"
LINKHUB_CSV = (
    HEADER
    + "known.com,$210,,60,\n"
    + "rejected.com,$100,,40,\n"
    + "ordered.com,$330,,50,\n"
    + "new.com,$150,$90,45,думаю брать\n"
    + "https://www.urlsite.com/blog/,$120,,30,\n"
    + "dup.com,180,,20,\n"
    + "dup.com,150,,20,\n"
    + ",$99,,10,\n"
)

UploadFactory = Callable[..., Upload]


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def collaborator() -> Seller:
    return Seller.collaborator()


@pytest.fixture
def linkhub() -> Seller:
    ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.1355"))
    return Seller.objects.create(name="LinkHub Media", currency="USD")


def _site_with_price(domain: str, seller: Seller, cents: int, currency: str = "EUR") -> Site:
    site = Site.objects.create(domain=domain)
    price = SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_cents=cents,
        currency=currency,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 9, 27)),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    return site


@pytest.fixture
def base(convertio: Product, collaborator: Seller, linkhub: Seller) -> dict[str, Site]:
    known = _site_with_price("known.com", collaborator, 20000)
    rejected = _site_with_price("rejected.com", collaborator, 15000)
    ProductSite.objects.filter(site=rejected, product=convertio).update(
        status=SiteStatus.DISCARDED, comment="Nofollow, отбрасываем"
    )
    ordered = _site_with_price("ordered.com", linkhub, 30000, "USD")
    Placement.objects.create(site=ordered, product=convertio, status=PlacementStatus.ORDERED)
    return {"known": known, "rejected": rejected, "ordered": ordered}


def _create(
    seller: Seller,
    content: str,
    *,
    name: str = "linkhub.csv",
    kind: UploadKind = UploadKind.PRICE_LIST,
    day: dt.date = PRICE_DATE,
) -> service.Created:
    file = SimpleUploadedFile(name, content.encode("utf-8"))
    return service.create_upload(file, kind=kind, seller=seller, prices_date=day)


def _check(upload: Upload) -> Upload:
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    upload.refresh_from_db()
    return upload


def _write(upload: Upload) -> Upload:
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk)
    upload.refresh_from_db()
    return upload


def _price_list(seller: Seller, content: str = LINKHUB_CSV, **kwargs: object) -> Upload:
    upload = _create(seller, content, **kwargs).upload  # type: ignore[arg-type]
    if service.needs_questions(upload):
        errors = service.confirm_mapping(upload, upload.mapping or {}, upload.currency or "USD")
        assert errors == []
    return upload


class TestFlow:
    def test_summary_before_write_shows_everything(
        self, base: dict[str, Site], linkhub: Seller
    ) -> None:
        upload = _check(_price_list(linkhub))
        assert upload.status == UploadStatus.CHECKED
        summary = upload.summary
        assert summary is not None
        assert (summary["sites"], summary["known"], summary["new"]) == (6, 3, 3)
        assert summary["currency"] == "USD"
        groups = summary["groups"]
        # known.com: $210 ≈ €184.94 < €200 у Collaborator.
        assert groups[ReviewGroup.CHEAPER] == 1
        assert groups[ReviewGroup.REJECTED] == 1
        assert groups[ReviewGroup.CHANGED] == 1  # ordered.com, заявка в работе
        assert groups[ReviewGroup.NEW] == 4  # new.com ×2, urlsite.com, dup.com
        assert summary["changed_up"] == 1
        assert summary["needs_decision"] == 3
        assert summary["rejected"] == [
            {"domain": "rejected.com", "reasons": ["Convertio: Отбрасываю — Nofollow, отбрасываем"]}
        ]
        assert summary["duplicates"] == [
            {"domain": "dup.com", "lines": [7, 8], "prices": ["публикация 180", "публикация 150"]}
        ]
        assert summary["errors"] == [{"line": 9, "message": "нет площадки"}]
        assert summary["urls"] == [
            {"line": 6, "value": "https://www.urlsite.com/blog/", "domain": "urlsite.com"}
        ]
        # Сводка базу не меняет.
        assert not Site.objects.filter(domain="new.com").exists()
        assert TaskRun.objects.get(task_name="upload_check").status == TaskStatus.SUCCESS

    def test_write_creates_offers_list_and_review(
        self, base: dict[str, Site], linkhub: Seller, collaborator: Seller
    ) -> None:
        upload = _write(_check(_price_list(linkhub)))
        assert upload.status == UploadStatus.DONE, upload.error
        assert TaskRun.objects.get(task_name="upload_write").status == TaskStatus.SUCCESS

        new = Site.objects.get(domain="new.com")
        assert new.source == "LinkHub Media"
        assert ProductSite.objects.get(site=new).status == SiteStatus.NEW
        # Публикация стала рабочей сама, вставка — рядом.
        assert new.price is not None
        assert (new.price.placement_type, new.price.placement_cents, new.price.currency) == (
            PlacementType.GUEST_POST,
            15000,
            "USD",
        )
        assert SitePrice.objects.filter(site=new).count() == 2
        assert SiteNote.objects.get(site=new).body == "думаю брать"
        metric = SiteMetric.objects.get(site=new)
        assert (metric.dr, metric.seller_id) == (45, linkhub.pk)
        assert Site.objects.get(domain="dup.com").price.placement_cents == 15000  # type: ignore[union-attr]

        # Рабочие цены известных площадок не тронуты, новые цены ждут решения.
        for site in base.values():
            old_price = site.price_id
            site.refresh_from_db()
            assert site.price_id == old_price
        pending = SitePrice.objects.filter(seller=linkhub, reviewed_at__isnull=True)
        assert {p.site.domain for p in pending} == {"known.com", "rejected.com", "ordered.com"}

        assert upload.site_list is not None
        assert upload.site_list.name == "LinkHub Media · 01.10.2026"
        items = SiteListItem.objects.filter(site_list=upload.site_list)
        assert items.count() == 6
        assert set(items.filter(first_seen=True).values_list("site__domain", flat=True)) == {
            "new.com",
            "urlsite.com",
            "dup.com",
        }
        review = UploadItem.objects.filter(upload=upload)
        assert review.count() == 7
        assert review.filter(needs_decision=True).count() == 3
        cheaper = review.get(review_group=ReviewGroup.CHEAPER)
        assert cheaper.ref_price_id == base["known"].price_id
        assert upload.result is not None
        assert upload.result["counts"]["sites_created"] == 3

    def test_same_file_again_opens_the_written_upload(
        self, base: dict[str, Site], linkhub: Seller
    ) -> None:
        first = _write(_check(_price_list(linkhub)))
        again = _create(linkhub, LINKHUB_CSV)
        assert again.duplicate
        assert again.upload.pk == first.pk
        assert Upload.objects.count() == 1

    def test_second_file_same_day_updates_without_duplicates(
        self, base: dict[str, Site], linkhub: Seller
    ) -> None:
        _write(_check(_price_list(linkhub)))
        before = SitePrice.objects.count()
        changed = LINKHUB_CSV.replace("known.com,$210", "known.com,$205")
        second = _price_list(linkhub, changed, name="linkhub-fixed.csv")
        # Те же заголовки — разметка запомнена у продавца, вопросов нет.
        assert not service.needs_questions(second)
        second = _write(_check(second))
        assert second.status == UploadStatus.DONE, second.error
        assert SitePrice.objects.count() == before
        known = SitePrice.objects.get(seller=linkhub, site__domain="known.com")
        assert known.placement_cents == 20500
        assert Site.objects.count() == 6

    def test_next_price_list_moves_working_price_of_same_seller(
        self, base: dict[str, Site], linkhub: Seller
    ) -> None:
        _write(_check(_price_list(linkhub)))
        later = HEADER + "new.com,$160,,45,\nordered.com,$340,,50,\n"
        upload = _write(_check(_price_list(linkhub, later, day=dt.date(2026, 10, 5))))
        new = Site.objects.get(domain="new.com")
        assert new.price is not None
        assert new.price.placement_cents == 16000  # пошла за продавцом сама
        assert UploadItem.objects.get(upload=upload, site=new).review_group == ReviewGroup.CHANGED
        # Заявка в работе — рабочая цена стоит, ждёт только последняя цена продавца.
        ordered = Site.objects.get(domain="ordered.com")
        assert ordered.price_id == base["ordered"].price_id
        pending = SitePrice.objects.filter(site=ordered, reviewed_at__isnull=True)
        assert list(pending.values_list("placement_cents", flat=True)) == [34000]

    def test_new_column_asks_one_question(self, base: dict[str, Site], linkhub: Seller) -> None:
        _price_list(linkhub)
        upload = _create(linkhub, "Website,GP Price,LI Price,DR,Комментарий,DA\nx.com,1,,1,,30\n")
        asked = [c["header"] for c in upload.upload.columns or [] if c["ask"]]
        assert asked == ["DA"]

    def test_currency_without_rate_is_refused(self, base: dict[str, Site], linkhub: Seller) -> None:
        upload = _create(linkhub, LINKHUB_CSV).upload
        errors = service.confirm_mapping(upload, upload.mapping or {}, "UAH")
        assert any("UAH" in e for e in errors)

    def test_broken_file_fails_with_message(self, linkhub: Seller) -> None:
        upload = _create(linkhub, "x", name="price.pdf").upload
        assert upload.status == UploadStatus.FAILED
        assert "не поддерживается" in (upload.error or "")


def _catalog_csv(rows: list[dict[str, str]]) -> str:
    headers = [*catalog.REQUIRED, "TF"]
    lines = [";".join(f'"{h}"' for h in headers)]
    for row in rows:
        lines.append(";".join(f'"{row.get(h, "")}"' for h in headers))
    return "\n".join(lines) + "\n"


def _catalog_row(domain: str, price: str, **extra: str) -> dict[str, str]:
    return {
        "Domain": f"https://www.{domain}",
        "Collaborator URL": f"https://collaborator.pro/{domain}",
        "Category": "Internet, Marketing",
        "Website languages": "English",
        "Website type": "Personal blog",
        "Link type article": "dofollow",
        "Advertising mark article": "No",
        "Number of links article": "2",
        "Publishing price article, EUR": price,
        "Announcement price article, EUR": "free",
        "DR": "50",
        "Organic traffic": "12000",
        "Keywords": "300",
        "TF": "20",
        **extra,
    }


class TestCatalog:
    def test_catalog_loads_without_mapping(
        self, base: dict[str, Site], collaborator: Seller
    ) -> None:
        content = _catalog_csv(
            [_catalog_row("known.com", "204.12"), _catalog_row("fresh.com", "90.00")]
        )
        created = _create(
            collaborator, content, name="collaborator.csv", kind=UploadKind.COLLABORATOR_CATALOG
        )
        upload = created.upload
        assert upload.status == UploadStatus.NEW, upload.error
        assert not service.needs_questions(upload)
        upload = _write(_check(upload))
        assert upload.status == UploadStatus.DONE, upload.error

        known = Site.objects.get(domain="known.com")
        # Цена Collaborator поплыла с курсом — рабочая пошла за ней сама.
        assert known.price is not None
        assert (known.price.placement_cents, known.price.announce_cents) == (20412, 0)
        assert known.topics == ["Интернет", "Маркетинг"]
        assert known.languages == ["Английский"]
        assert known.language == "en"
        assert known.price.extra == {"TF": "20"}

        fresh = Site.objects.get(domain="fresh.com")
        assert fresh.source == "Collaborator"
        assert fresh.site_type == "Персональный блог"
        assert ProductSite.objects.get(site=fresh).status == SiteStatus.NEW
        metric = SiteMetric.objects.get(site=fresh)
        assert (metric.dr, metric.organic_traffic, metric.seller_id) == (50, 12000, collaborator.pk)
        assert upload.site_list is not None
        assert upload.site_list.name == "Collaborator · 01.10.2026"

    def test_not_a_catalog_export(self, collaborator: Seller) -> None:
        upload = _create(
            collaborator, "Domain;Price\na.com;1\n", kind=UploadKind.COLLABORATOR_CATALOG
        ).upload
        assert upload.status == UploadStatus.FAILED
        assert "Collaborator URL" in (upload.error or "")
