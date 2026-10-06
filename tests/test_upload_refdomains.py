"""Ссылающиеся домены Ahrefs: загрузка, «Площадки», сводки, карточка (E1-09, ADR-051).

Файл — как Export на странице Referring domains: UTF-16 с табуляцией и
кавычками, заголовок «Traffic » с пробелом в конце.
"""

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.urls import reverse

from apps.sites.models import (
    Product,
    ProductRefDomain,
    Seller,
    Site,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import refdomains, service
from apps.sites.uploads.files import read_table
from apps.workspace.products import choose_product

pytestmark = pytest.mark.django_db

HEADER = [
    "Domain", "Is spam", "DR", "Dofollow ref. domains", "Dofollow linked domains", "Traffic ",
    "Keywords ", "Links to target", "Dofollow links", "First seen", "Lost",
]  # fmt: skip


def _export(rows: Sequence[tuple[str, str, str]]) -> bytes:
    """Строки — (домен, First seen, Lost)."""
    lines = ["\t".join(f'"{h}"' for h in HEADER)]
    for domain, first_seen, lost in rows:
        cells = [domain, "false", "70", "10", "5", "1000", "100", "2", "1", first_seen, lost]
        lines.append("\t".join(f'"{c}"' for c in cells))
    return ("\n".join(lines) + "\n").encode("utf-16")


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo(convertio: Product) -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _load(product: Product, rows: Sequence[tuple[str, str, str]], day: dt.date) -> Upload:
    file = SimpleUploadedFile(f"refdomains-{day}.csv", _export(rows))
    upload = service.create_upload(
        file, kind=UploadKind.REF_DOMAINS, seller=None, product=product, prices_date=day
    ).upload
    assert upload.status == UploadStatus.NEW, upload.error
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.CHECKED, upload.error
    return upload


def _write(upload: Upload) -> Upload:
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


ROWS = [
    ("google.com", "2023-08-10 15:51:35", ""),
    ("www.Known.com", "2025-01-02 10:00:00", ""),
    ("gone.com", "2024-05-01 00:00:00", "2026-09-01 12:00:00"),
]
OCT_1 = dt.date(2026, 10, 1)


class TestUpload:
    def test_summary_and_write(self, convertio: Product) -> None:
        Site.objects.create(domain="known.com")
        upload = _load(convertio, ROWS, OCT_1)
        summary = upload.summary
        assert summary is not None
        assert (summary["domains"], summary["new"], summary["known"]) == (3, 3, 0)
        assert summary["in_sites"] == [{"domain": "known.com"}]
        assert summary["lost_in_file"] == 1
        assert not ProductRefDomain.objects.exists()  # сводка базу не меняет

        _write(upload)
        rows = {row.domain: row for row in ProductRefDomain.objects.filter(product=convertio)}
        assert set(rows) == {"google.com", "known.com", "gone.com"}
        known = rows["known.com"]
        assert known.first_seen_at == dt.datetime(2025, 1, 2, 10, tzinfo=dt.UTC)
        assert (known.seen_on, known.is_linking, known.upload_id) == (OCT_1, True, upload.pk)
        assert rows["gone.com"].lost_at == dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)
        assert not rows["gone.com"].is_linking
        # В «Площадки» домены выгрузки не попадают.
        assert not Site.objects.filter(domain="google.com").exists()
        # Статистика таблицы обновлена сразу после записи — для плана фильтра «Площадок».
        with connection.cursor() as cursor:
            cursor.execute("SELECT reltuples FROM pg_class WHERE relname = 'product_ref_domains'")
            assert cursor.fetchone() == (3.0,)

    def test_newer_export_marks_missing_older_does_not(self, convertio: Product) -> None:
        _write(_load(convertio, ROWS, OCT_1))
        later = dt.date(2026, 10, 15)
        upload = _load(convertio, ROWS[:1], later)
        assert upload.summary is not None
        assert upload.summary["missing"] == [{"domain": "known.com"}]  # gone.com уже пропал
        _write(upload)
        known = ProductRefDomain.objects.get(domain="known.com")
        assert (known.missing_since, known.is_linking) == (later, False)

        # Домен вернулся в следующей выгрузке — пометка снимается.
        _write(_load(convertio, ROWS[:2], dt.date(2026, 10, 20)))
        known.refresh_from_db()
        assert (known.missing_since, known.seen_on) == (None, dt.date(2026, 10, 20))

        # Старая выгрузка после новой только добавляет: пропажу не отмечает, даты назад не идут.
        old = _load(convertio, [("new-old.com", "2022-01-01 00:00:00", "")], dt.date(2026, 9, 1))
        assert old.summary is not None and old.summary["newest"] is False
        _write(old)
        known.refresh_from_db()
        assert (known.missing_since, known.seen_on) == (None, dt.date(2026, 10, 20))
        assert ProductRefDomain.objects.get(domain="new-old.com").seen_on == dt.date(2026, 9, 1)

    def test_not_an_export(self, convertio: Product) -> None:
        file = SimpleUploadedFile("x.csv", b"Website,Price\na.com,10\n")
        upload = service.create_upload(
            file, kind=UploadKind.REF_DOMAINS, seller=None, product=convertio, prices_date=OCT_1
        ).upload
        assert upload.status == UploadStatus.FAILED
        assert "Referring domains" in (upload.error or "")

    def test_parse_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "refdomains.csv"
        path.write_bytes(_export([("ok.com", "2023-01-01 00:00:00", ""), ("bad.com", "вчера", "")]))
        parsed = refdomains.parse(read_table(path, is_header=refdomains.looks_like_refdomains))
        assert [ref.domain for ref in parsed.domains] == ["ok.com"]
        assert parsed.errors[0].message == "не дата: 'вчера'"


class TestSitesScreen:
    @pytest.fixture
    def linked(self, convertio: Product, clideo: Product) -> None:
        for domain in ("linking.com", "lost.com", "plain.com"):
            Site.objects.create(domain=domain)
        ProductRefDomain.objects.create(product=convertio, domain="linking.com", seen_on=OCT_1)
        ProductRefDomain.objects.create(
            product=convertio, domain="lost.com", seen_on=OCT_1, missing_since=OCT_1
        )

    def _domains(self, client: Client, query: str) -> set[str]:
        url = reverse("admin:sites_productsitelatest_changelist") + query
        response = client.get(url)
        return {row.domain for row in response.context["cl"].result_list}

    @pytest.mark.usefixtures("linked")
    def test_linking_sites_are_hidden_for_the_product_only(
        self, admin_client: Client, admin_user: User, convertio: Product, clideo: Product
    ) -> None:
        # Продукт строк — рабочий, из шапки (ADR-063); Convertio первый активный.
        base = "?list=all"
        assert self._domains(admin_client, base) == {"lost.com", "plain.com"}
        assert self._domains(admin_client, base + "&refs=only") == {"linking.com"}
        assert self._domains(admin_client, base + "&refs=lost") == {"lost.com"}
        assert self._domains(admin_client, base + "&refs=all") == {
            "linking.com",
            "lost.com",
            "plain.com",
        }
        # У другого продукта ничего не прячется.
        choose_product(admin_user, clideo)
        assert self._domains(admin_client, "?list=all") == {
            "linking.com",
            "lost.com",
            "plain.com",
        }

    @pytest.mark.usefixtures("linked")
    def test_card_says_it_links(self, admin_client: Client) -> None:
        site = Site.objects.get(domain="linking.com")
        page = admin_client.get(reverse("admin:sites_site_card", args=[site.pk])).content.decode()
        assert 'title="По выгрузке Ahrefs от 01.10.2026">ссылается на Convertio' in page
        lost = Site.objects.get(domain="lost.com")
        page = admin_client.get(reverse("admin:sites_site_card", args=[lost.pk])).content.decode()
        assert "ссылки на Convertio нет в выгрузке Ahrefs с 01.10.2026" in page

    @pytest.mark.usefixtures("linked")
    def test_refdomains_list(self, admin_client: Client) -> None:
        url = reverse("admin:sites_productrefdomain_changelist")
        page = admin_client.get(url + "?state=lost").content.decode()
        assert "lost.com" in page and "linking.com" not in page
        assert "карточка" in page

    def test_price_list_summary_says_who_links(
        self, admin_client: Client, convertio: Product
    ) -> None:
        ProductRefDomain.objects.create(product=convertio, domain="linking.com", seen_on=OCT_1)
        seller = Seller.objects.create(name="LinkHub Media", currency="EUR")
        file = SimpleUploadedFile("p.csv", b"Website,GP Price\nlinking.com,100\nother.com,50\n")
        upload = service.create_upload(
            file, kind=UploadKind.PRICE_LIST, seller=seller, prices_date=OCT_1
        ).upload
        if service.needs_questions(upload):
            assert service.confirm_mapping(upload, upload.mapping or {}, "EUR") == []
        service.start(upload, UploadStatus.CHECKING)
        upload_check.delay(upload.pk)
        upload.refresh_from_db()
        assert upload.summary is not None
        assert upload.summary["ref_domains"] == [
            {
                "product_id": convertio.pk,
                "product": "Convertio",
                "total": 1,
                "domains": ["linking.com"],
            }
        ]
        page = admin_client.get(reverse("admin:sites_upload_summary", args=[upload.pk]))
        assert "Уже ссылаются на Convertio: 1" in page.content.decode()


def test_upload_form_needs_product(admin_client: Client, convertio: Product) -> None:
    file = SimpleUploadedFile("r.csv", _export(ROWS))
    response = admin_client.post(
        reverse("admin:sites_upload_add"),
        {"kind": "ahrefs_refdomains", "prices_date": "2026-10-01", "file": file},
    )
    assert "Выберите продукт" in response.content.decode()
    file = SimpleUploadedFile("r.csv", _export(ROWS))
    response = admin_client.post(
        reverse("admin:sites_upload_add"),
        {
            "kind": "ahrefs_refdomains",
            "product": convertio.pk,
            "prices_date": "2026-10-01",
            "file": file,
        },
    )
    upload = Upload.objects.get()
    assert (upload.product, upload.seller) == (convertio, None)
    assert response["Location"] == reverse("admin:sites_upload_summary", args=[upload.pk])
    page = admin_client.get(response["Location"]).content.decode()
    assert "Кто ссылается на <b>Convertio</b>" in page
