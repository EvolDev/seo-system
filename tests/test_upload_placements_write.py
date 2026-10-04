"""Загрузка файла размещений: сводка до записи и запись (E1-09, ADR-051).

Через сервис и задачи очереди (в тестах они выполняются сразу). Файл — CSV
с заголовками как у `clideo_через_барыг.xlsx`; разбор строк по отдельности —
`test_upload_placements_parse.py`.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.keywords.models import Keyword
from apps.observability.models import Check, CheckStatus, Performer
from apps.placements.models import Placement, PlacementLink, PlacementStatus, PlacementStatusChange
from apps.sites.models import (
    ExchangeRate,
    MetricSource,
    PlacementType,
    Product,
    ProductSite,
    ProductSiteLatest,
    Seller,
    Site,
    SiteListItem,
    SiteNote,
    SitePrice,
    SiteStatus,
    SiteStatusChange,
    StatusSource,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import service
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

FILE_DATE = dt.date(2026, 10, 4)
HEADERS = [
    "Target", "Person", "Source", "URL статьи", "Индексация", "Статус", "Дата размещения",
    "Комментарий", "Анкор1", "Ссылка1", "Анкор2", "Ссылка2", "Traffic", "Итог цена",
    "Индексация 02.09.26",
]  # fmt: skip


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo(convertio: Product) -> Product:
    product = Product.objects.create(name="Clideo", domain="clideo.com")
    Keyword.objects.create(
        product=product, keyword="video editor", target_url="https://clideo.com/video-editor"
    )
    return product


@pytest.fixture
def author() -> User:
    return User.objects.create_user("alice", password="x", is_staff=True)


def _csv(rows: Sequence[Mapping[str, str]], headers: Sequence[str] = HEADERS) -> str:
    lines = [";".join(headers)]
    lines += [";".join(row.get(header, "") for header in headers) for row in rows]
    return "\n".join(lines) + "\n"


def _upload(
    product: Product,
    rows: Sequence[Mapping[str, str]],
    *,
    headers: Sequence[str] = HEADERS,
    name: str = "clideo.csv",
    seller: Seller | None = None,
    employee: User | None = None,
    author: User | None = None,
) -> Upload:
    file = SimpleUploadedFile(name, _csv(rows, headers).encode("utf-8"))
    created = service.create_upload(
        file,
        kind=UploadKind.PLACEMENTS,
        seller=seller,
        product=product,
        employee=employee,
        prices_date=FILE_DATE,
        author=author,
    )
    upload = created.upload
    if service.needs_questions(upload):
        assert service.confirm_mapping(upload, upload.mapping or {}, upload.currency or "EUR") == []
    return upload


def _check(upload: Upload) -> Upload:
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.CHECKED, upload.error
    return upload


def _write(upload: Upload, action: str = "all") -> Upload:
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk, action)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


TOMSGUIDE = {
    "Target": "tomsguide.com",
    "Person": "Evgeniy",
    "Source": "Athena Smith",
    "URL статьи": "https://www.tomsguide.com/computing/ai-video",
    "Индексация": "да",
    "Статус": "Размещено",
    "Дата размещения": "14.09.2026",
    "Комментарий": "Link insert",
    "Анкор1": "video editor",
    "Ссылка1": "https://clideo.com/video-editor",
    "Анкор2": "App Store",
    "Ссылка2": "https://apps.apple.com/us/app/clideo/id1552262611",
    "Traffic": "4 086 679",
    "Итог цена": "311,81",
    "Индексация 02.09.26": "#Н/Д",
}
VOCAL = {
    "Target": "vocal.media",
    "Person": "Lilith",
    "Source": "maher it firm",
    "URL статьи": "https://vocal.media/01/ai-productivity",
    "Индексация": "да",
    "Статус": "",
    "Дата размещения": "19.08.2026",
    "Комментарий": "Размещено вне коллаборатора",
    "Анкор1": "https://clideo.com/video-maker",
    "Ссылка1": "online video maker",
    "Итог цена": "163",
    "Индексация 02.09.26": "нет",
}
BIG_PRICE = {
    "Target": "squaremile.com",
    "Source": "uniguide",
    "URL статьи": "https://squaremile.com/business/video",
    "Статус": "Размещено",
    "Дата размещения": "1.23.2026",
    "Итог цена": "4330755",
}


def _site_with_price(domain: str, seller: Seller, cents: int, day: dt.date) -> Site:
    site = Site.objects.create(domain=domain)
    price = SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_cents=cents,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(day),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    return site


class TestSummary:
    def test_summary_before_write(self, clideo: Product) -> None:
        # vocal.media уже в базе: статья Clideo из таблицы, у неё продавец другой.
        site = Site.objects.create(domain="vocal.media")
        old = Seller.objects.create(name="Old Seller")
        Placement.objects.create(
            site=site,
            product=clideo,
            status=PlacementStatus.PUBLISHED,
            article_url="https://vocal.media/01/ai-productivity/",
            seller=old,
        )
        bad = {"Target": "bookafy.com", "URL статьи": "https://www.dashclicks.com/blog/x"}
        upload = _check(_upload(clideo, [TOMSGUIDE, VOCAL, BIG_PRICE, bad]))
        summary = upload.summary
        assert summary is not None
        assert (summary["sites"], summary["known"], summary["new"]) == (3, 1, 2)
        assert summary["placements_new"] == 2
        assert summary["placements_supplemented"] == 1
        assert summary["no_status"] == 1
        assert summary["sellers_new"] == ["Athena Smith", "maher it firm", "uniguide"]
        assert summary["employees_new"] == ["Evgeniy", "Lilith"]
        assert summary["links_new"] == 3  # vocal.media — ссылка новая, анкор и адрес — местами
        assert summary["links_swapped"][0]["domain"] == "vocal.media"
        assert summary["links_foreign_total"] == 1  # App Store
        assert summary["bad_dates"][0]["message"].startswith("дата «1.23.2026»")
        assert summary["price_cap"] == [
            {
                "line": 4,
                "domain": "squaremile.com",
                "what": "Заплачено",
                "value": "4 330 755,00 EUR",
            }
        ]
        assert summary["conflicts"] == [
            {
                "line": 3,
                "domain": "vocal.media",
                "what": "продавец",
                "base": "Old Seller",
                "file": "maher it firm",
            }
        ]
        assert summary["errors_total"] == 1
        assert "не адрес статьи на bookafy.com" in summary["errors"][0]["message"]
        assert summary["checks_new"] == 3  # tomsguide — одна, vocal — две
        # Сводка базу не меняет.
        assert not Site.objects.filter(domain="tomsguide.com").exists()
        assert Placement.objects.count() == 1


class TestWrite:
    def test_new_placements_and_everything_around(
        self, clideo: Product, convertio: Product, author: User
    ) -> None:
        upload = _write(_check(_upload(clideo, [TOMSGUIDE, VOCAL], author=author)))
        placement = Placement.objects.get(site__domain="tomsguide.com")
        assert placement.product == clideo
        assert placement.status == PlacementStatus.PUBLISHED
        assert placement.article_url == TOMSGUIDE["URL статьи"]
        assert placement.published_at is not None
        assert timezone.localtime(placement.published_at).date() == dt.date(2026, 9, 14)
        assert placement.placement_type == PlacementType.LINK_INSERTION
        assert (placement.price_paid_cents, placement.currency) == (31181, "EUR")
        assert placement.seller is not None and placement.seller.name == "Athena Smith"
        assert placement.employee is not None and placement.employee.first_name == "Evgeniy"
        assert placement.extra == {"Traffic": "4 086 679"}
        links = {link.link_index: link for link in placement.links.select_related("keyword")}
        assert links[1].keyword is not None and links[1].keyword.keyword == "video editor"
        assert links[2].target_url.startswith("https://apps.apple.com/")
        # Отметка «да» на дату файла; «#Н/Д» — не отметка.
        [check] = Check.objects.filter(entity_id=placement.pk, check_type="indexation")
        assert (check.status, check.performed_by) == (CheckStatus.OK, Performer.HUMAN)
        assert check.checked_at == start_of_day(FILE_DATE)
        assert check.result is not None and check.result["upload_id"] == upload.pk
        assert placement.is_indexed is True

        vocal = Placement.objects.get(site__domain="vocal.media")
        assert vocal.status == PlacementStatus.PUBLISHED  # пустой статус — «Опубликовано»
        [link] = vocal.links.all()
        assert (link.anchor, link.target_url) == (
            "online video maker",
            "https://clideo.com/video-maker",
        )
        statuses = [
            (c.checked_at, c.status)
            for c in Check.objects.filter(entity_id=vocal.pk).order_by("checked_at")
        ]
        assert statuses == [
            (start_of_day(dt.date(2026, 9, 2)), CheckStatus.FAILED),
            (start_of_day(FILE_DATE), CheckStatus.OK),
        ]
        assert vocal.is_indexed is True
        note = SiteNote.objects.get(site__domain="vocal.media")
        assert (note.body, note.product) == ("Размещено вне коллаборатора", clideo)
        assert not SiteNote.objects.filter(site__domain="tomsguide.com").exists()  # Link insert

        # Сотрудники — пользователи без права входа.
        evgeniy = User.objects.get(first_name="Evgeniy")
        assert not evgeniy.has_usable_password()
        assert not evgeniy.is_staff

        # Площадка у Clideo — «Размещались», в истории — «загрузка» от того, кто загрузил.
        row = ProductSite.objects.get(site__domain="tomsguide.com", product=clideo)
        assert row.status == SiteStatus.PLACED
        change = SiteStatusChange.objects.get(product_site=row)
        assert (change.actor_id, change.placement_id) == (author.pk, placement.pk)
        history = PlacementStatusChange.objects.get(placement=placement)
        assert (history.source, history.actor_id) == (StatusSource.UPLOAD, author.pk)
        # У Convertio — «Новая» и «другие продукты: Clideo».
        latest = ProductSiteLatest.objects.get(site__domain="tomsguide.com", product=convertio)
        assert latest.status == SiteStatus.NEW
        assert latest.other_products_placed == ["Clideo"]

        # Цена Clideo — предложение продавца на дату размещения; у новой площадки — рабочее.
        site = Site.objects.get(domain="tomsguide.com")
        assert site.price is not None
        assert (site.price.seller.name, site.price.placement_cents) == ("Athena Smith", 31181)
        assert site.price.placement_type == PlacementType.LINK_INSERTION
        assert site.price.checked_at == start_of_day(dt.date(2026, 9, 14))
        assert site.price.reviewed_at is not None
        # Все площадки файла — в рабочем списке загрузки.
        assert upload.site_list is not None
        assert upload.site_list.name == "Clideo · размещения · 04.10.2026"
        assert SiteListItem.objects.filter(site_list=upload.site_list).count() == 2

    def test_placement_from_table_is_completed_not_duplicated(self, clideo: Product) -> None:
        site = Site.objects.create(domain="tomsguide.com")
        placement = Placement.objects.create(
            site=site,
            product=clideo,
            status=PlacementStatus.PUBLISHED,
            article_url="https://tomsguide.com/computing/ai-video/",
        )
        _write(_check(_upload(clideo, [TOMSGUIDE])))
        [found] = Placement.objects.filter(product=clideo)
        assert found.pk == placement.pk
        assert found.article_url == "https://tomsguide.com/computing/ai-video/"
        assert found.seller is not None and found.seller.name == "Athena Smith"
        assert found.price_paid_cents == 31181
        assert found.links.count() == 2

    def test_domain_list_from_seller(self, clideo: Product) -> None:
        seller = Seller.objects.create(name="Zain Mediax")
        known = Site.objects.create(domain="known.com")
        with_url = Placement.objects.create(
            site=known,
            product=clideo,
            status=PlacementStatus.PUBLISHED,
            article_url="https://known.com/post",
        )
        rows = [{"Сайт": "known.com"}, {"Сайт": "https://www.fresh.com/"}]
        upload = _write(_check(_upload(clideo, rows, headers=["Сайт"], seller=seller)))
        assert upload.status == UploadStatus.DONE
        with_url.refresh_from_db()
        assert with_url.seller == seller  # то же размещение, не второе
        fresh = Placement.objects.get(site__domain="fresh.com")
        assert (fresh.article_url, fresh.status, fresh.seller) == (
            None,
            PlacementStatus.PUBLISHED,
            seller,
        )
        assert Placement.objects.filter(product=clideo).count() == 2

    def test_domain_list_without_header_from_employee(self, clideo: Product) -> None:
        artem = User.objects.create_user("artem", first_name="Artem")
        file = SimpleUploadedFile("artem.csv", b"one.com\nhttps://two.com/\n")
        upload = service.create_upload(
            file,
            kind=UploadKind.PLACEMENTS,
            seller=None,
            product=clideo,
            employee=artem,
            prices_date=FILE_DATE,
        ).upload
        assert upload.header_row == 0
        assert [c["header"] for c in upload.columns or []] == ["Площадка"]
        assert (
            not service.needs_questions(upload)
            or service.confirm_mapping(upload, upload.mapping or {}, "EUR") == []
        )
        _write(_check(upload))
        placements = Placement.objects.filter(product=clideo).order_by("site__domain")
        assert [(p.site.domain, p.employee) for p in placements] == [
            ("one.com", artem),
            ("two.com", artem),
        ]

    def test_repeat_and_updated_file(self, clideo: Product) -> None:
        upload = _write(_check(_upload(clideo, [TOMSGUIDE, VOCAL])))
        totals = (
            Placement.objects.count(),
            PlacementLink.objects.count(),
            Check.objects.count(),
            SiteNote.objects.count(),
            SitePrice.objects.count(),
            Seller.objects.count(),
            User.objects.count(),
        )
        # Тот же файл с той же датой — прежняя загрузка.
        again = service.create_upload(
            SimpleUploadedFile("clideo.csv", _csv([TOMSGUIDE, VOCAL]).encode("utf-8")),
            kind=UploadKind.PLACEMENTS,
            seller=None,
            product=clideo,
            prices_date=FILE_DATE,
        )
        assert again.duplicate and again.upload.pk == upload.pk
        # Обновлённый файл: другая цена — расхождение, без галочки не тронуто.
        changed = {**TOMSGUIDE, "Итог цена": "300"}
        second = _check(_upload(clideo, [changed, VOCAL], name="clideo-2.csv"))
        assert second.summary is not None
        assert second.summary["placements_new"] == 0
        assert second.summary["conflicts"][0]["what"] == "заплачено"
        second = _write(second)
        assert totals[:4] == (
            Placement.objects.count(),
            PlacementLink.objects.count(),
            Check.objects.count(),
            SiteNote.objects.count(),
        )
        assert Seller.objects.count() == totals[5]
        assert User.objects.count() == totals[6]
        placement = Placement.objects.get(site__domain="tomsguide.com")
        assert placement.price_paid_cents == 31181
        # С галочкой «Заменить расходящиеся» — значение из файла.
        third = _write(_check(_upload(clideo, [changed], name="clideo-3.csv")), "replace")
        placement.refresh_from_db()
        assert placement.price_paid_cents == 30000
        assert third.result is not None and third.result["runs"][0]["action"] == "replace"


class TestOffers:
    @pytest.fixture
    def collaborator(self) -> Seller:
        return Seller.collaborator()

    def test_working_price_rules(
        self, clideo: Product, convertio: Product, collaborator: Seller
    ) -> None:
        athena = Seller.objects.create(name="Athena Smith")
        # Рабочая цена Collaborator — рядом ляжет цена из размещения, справочно.
        catalog = _site_with_price("catalog.com", collaborator, 20000, dt.date(2026, 10, 2))
        # Рабочая — того же продавца и услуги, старее: перейдёт на цену из размещения.
        same = _site_with_price("same.com", athena, 25000, dt.date(2026, 1, 10))
        # То же, но по площадке заявка в работе — рабочая не двигается.
        frozen = _site_with_price("frozen.com", athena, 25000, dt.date(2026, 1, 10))
        Placement.objects.create(site=frozen, product=convertio, status=PlacementStatus.ORDERED)
        rows = [
            {
                "Target": domain,
                "Source": "Athena Smith",
                "Дата размещения": "14.09.2026",
                "Итог цена": "200",
            }
            for domain in ("catalog.com", "same.com", "frozen.com")
        ] + [{"Target": "via-collab.com", "Source": "Collaborator", "Итог цена": "90"}]
        _write(_check(_upload(clideo, rows)))
        catalog.refresh_from_db()
        assert catalog.price is not None and catalog.price.seller == collaborator
        beside = SitePrice.objects.get(site=catalog, seller=athena)
        assert (beside.placement_cents, beside.reviewed_at is not None) == (20000, True)
        same.refresh_from_db()
        assert same.price is not None and same.price.placement_cents == 20000
        frozen.refresh_from_db()
        assert frozen.price is not None and frozen.price.placement_cents == 25000
        # Цен Collaborator из размещений нет: его цены — из каталога.
        assert not SitePrice.objects.filter(site__domain="via-collab.com").exists()
        assert Placement.objects.get(site__domain="via-collab.com").seller == collaborator

    def test_price_in_dollars_over_the_cap(self, clideo: Product) -> None:
        ExchangeRate.objects.create(currency="USD", rate_date=FILE_DATE, rate=Decimal("1.13"))
        headers = ["Target", "Source", "Цена, $"]
        rows = [
            {"Target": "a.com", "Source": "Fajr Zee", "Цена, $": "324601"},
            {"Target": "b.com", "Source": "Fajr Zee", "Цена, $": "325"},
        ]
        upload = _check(_upload(clideo, rows, headers=headers))
        assert upload.currency == "USD"
        assert upload.summary is not None and upload.summary["price_cap_total"] == 1
        _write(upload)
        a = Placement.objects.get(site__domain="a.com")
        assert a.price_paid_cents is None
        assert not SitePrice.objects.filter(site__domain="a.com").exists()
        b = Placement.objects.get(site__domain="b.com")
        assert (b.price_paid_cents, b.currency) == (32500, "USD")


def test_human_mark_does_not_hide_newer_system_check(clideo: Product) -> None:
    site = Site.objects.create(domain="tomsguide.com")
    checked = timezone.now()
    placement = Placement.objects.create(
        site=site,
        product=clideo,
        status=PlacementStatus.PUBLISHED,
        article_url=TOMSGUIDE["URL статьи"],
        is_indexed=False,
        indexed_checked_at=checked,
    )
    # Отметка «да» — на дату файла, раньше сегодняшней проверки системы.
    _write(_check(_upload(clideo, [{**TOMSGUIDE, "Индексация": "да"}])))
    placement.refresh_from_db()
    assert (placement.is_indexed, placement.indexed_checked_at) == (False, checked)
    assert Check.objects.filter(entity_id=placement.pk, performed_by=Performer.HUMAN).count() == 1
