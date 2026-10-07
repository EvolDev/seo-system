"""Выгрузки Ahrefs Batch Analysis: разбор, сводка, запись (E1-10, ADR-045).

Файлы — как их отдаёт Export в Ahrefs: UTF-16 с меткой порядка байт,
табуляция, все значения в кавычках. Заголовки и значения строк — из
настоящих выгрузок 01.10.2026 (`my_source/`, в git не идут).
"""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.observability.models import TaskRun, TaskStatus
from apps.sites.models import (
    MetricSource,
    Product,
    ProductSiteLatest,
    Seller,
    Site,
    SiteCountryLatest,
    SiteCountryMetric,
    SiteLatest,
    SiteMetric,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import ahrefs, review, service
from apps.sites.uploads.files import read_table
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

DAY = dt.date(2026, 10, 1)
HEAD = (
    "#\tTarget\tMode\tIP\tProtocol\tURL Rating\tDomain Rating\tAhrefs Rank\t"
    "Organic / Total Keywords\tOrganic / Keywords (Top 3)\tOrganic / Keywords (4-10)\t"
    "Organic / Keywords (11-20)\tOrganic / Keywords (21-50)\tOrganic / Keywords (51+)\t"
    "Organic / Traffic\tOrganic / Value\t{top}Paid / Keywords\tPaid / Ads\tPaid / Traffic\t"
    "Paid / Cost\tRef. domains / All\tRef. domains / Followed\tRef. domains / Not followed\t"
    "Ref. IPs / IPs\tRef. IPs / Subnets\tBacklinks / All\tBacklinks / Followed\t"
    "Backlinks / Not followed\tBacklinks / Redirects\tBacklinks / Internal\t"
    "Outgoing domains / Followed\tOutgoing domains / All time\tOutgoing links / Followed\t"
    "Outgoing links / All time"
)
# Target, Mode, DR, Total Keywords, Traffic, Top Countries — остальное как в файле.
EGG = ("eggradients.com/", "subdomains", "55", "16245", "99979", "(us, 53814)")
EGG_US = ("eggradients.com/", "subdomains", "55", "13175", "53814", "")
CMD = ("commandlinux.com/", "subdomains", "59", "3966", "178262", "(us, 52637)")
UG = ("independent.co.ug/", "subdomains", "72", "2183", "25422", "(ug, 9236)")


def _line(number: int, row: tuple[str, ...], *, all_countries: bool) -> str:
    target, mode, dr, keywords, traffic, top = row
    cells = [str(number), target, mode, "198.202.211.1", "both", "4.5", dr, "466993", keywords]
    cells += ["3547", "10750", "1467", "481", "", traffic, "12611.27"]
    if all_countries:
        cells.append(top)
    cells += ["", "", "", "", "3709", "2625", "1099", "4540", "686", "51859", "47015"]
    cells += ["4844", "369", "735482", "972", "1038", "59108", "61426"]
    return "\t".join(f'"{cell}"' for cell in cells)


def batch(*rows: tuple[str, ...], all_countries: bool = True) -> bytes:
    head = HEAD.format(top="Organic / Top Countries\t" if all_countries else "")
    lines = ["\t".join(f'"{h}"' for h in head.split("\t"))]
    lines += [_line(n, row, all_countries=all_countries) for n, row in enumerate(rows, start=1)]
    return ("\n".join(lines) + "\n").encode("utf-16")


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def sites() -> dict[str, Site]:
    Product.objects.create(name="Convertio", domain="convertio.co")
    return {
        domain: Site.objects.create(domain=domain)
        for domain in ("eggradients.com", "commandlinux.com")
    }


def _create(
    content: bytes, *, country: str | None = None, day: dt.date = DAY, name: str = "batch.csv"
) -> service.Created:
    file = SimpleUploadedFile(name, content)
    return service.create_upload(
        file, kind=UploadKind.AHREFS_BATCH, seller=None, prices_date=day, country=country
    )


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


def _load(content: bytes, **kwargs: object) -> Upload:
    upload = _write(_check(_create(content, **kwargs).upload))  # type: ignore[arg-type]
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


class TestParse:
    def test_all_countries_row(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.csv"
        path.write_bytes(batch(EGG))
        table = read_table(path, is_header=ahrefs.looks_like_batch)
        assert ahrefs.missing_columns(table) == []
        assert ahrefs.is_all_countries(table)
        parsed = ahrefs.parse(table)
        assert parsed.errors == []
        (egg,) = parsed.measures
        assert (egg.domain, egg.source_value, egg.dr) == ("eggradients.com", None, 55)
        assert (egg.organic_traffic, egg.total_keywords) == (99979, 16245)
        assert (egg.top_geo, egg.top_geo_traffic) == ("us", 53814)
        # Всё остальное — в сырые данные, кроме номера строки; пустые — не пишутся.
        assert egg.raw["URL Rating"] == "4.5"
        assert egg.raw["Ahrefs Rank"] == "466993"
        assert egg.raw["Organic / Value"] == "12611.27"
        assert "#" not in egg.raw
        assert "Paid / Keywords" not in egg.raw

    def test_country_export_has_no_top_countries(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.csv"
        path.write_bytes(batch(EGG_US, all_countries=False))
        table = read_table(path, is_header=ahrefs.looks_like_batch)
        assert not ahrefs.is_all_countries(table)
        (egg,) = ahrefs.parse(table).measures
        assert (egg.organic_traffic, egg.total_keywords, egg.top_geo) == (53814, 13175, None)

    def test_bad_rows_go_to_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "batch.csv"
        exact = ("convertio.co/blog/", "exact", "80", "10", "100", "(us, 50)")
        broken = ("broken.com/", "subdomains", "много", "1", "1", "(us, 1)")
        geo = ("geo.com/", "subdomains", "50", "1", "1", "us 1")
        empty = ("empty.com/", "subdomains", "30", "", "", "")
        path.write_bytes(batch(EGG, exact, broken, geo, empty, EGG))
        parsed = ahrefs.parse(read_table(path, is_header=ahrefs.looks_like_batch))
        assert [(e.line, e.message.split(":")[0]) for e in parsed.errors] == [
            (3, "convertio.co"),
            (4, "«Domain Rating»"),
            (5, "«Organic / Top Countries»"),
        ]
        assert "режим «exact»" in parsed.errors[0].message
        # Пусто у Ahrefs — нет данных, а не ноль.
        empty_measure = next(m for m in parsed.measures if m.domain == "empty.com")
        assert (empty_measure.organic_traffic, empty_measure.top_geo) == (None, None)
        assert parsed.duplicates == [("eggradients.com", (2, 7))]


class TestCountryChoice:
    def test_country_chosen_for_all_countries_file(self) -> None:
        upload = _create(batch(EGG), country="us").upload
        assert upload.status == UploadStatus.FAILED
        assert "выгрузка по всем странам" in (upload.error or "")
        assert "США" in (upload.error or "")

    def test_all_chosen_for_country_file(self) -> None:
        upload = _create(batch(EGG_US, all_countries=False)).upload
        assert upload.status == UploadStatus.FAILED
        assert "выгрузка по одной стране" in (upload.error or "")

    def test_not_a_batch_file(self) -> None:
        upload = _create(b"Domain,DR\nsite.com,50\n").upload
        assert upload.status == UploadStatus.FAILED
        assert "Не похоже на выгрузку Ahrefs Batch Analysis" in (upload.error or "")

    def test_matching_choice_needs_no_questions(self) -> None:
        upload = _create(batch(EGG_US, all_countries=False), country="us").upload
        assert upload.status == UploadStatus.NEW
        assert upload.seller_id is None
        assert upload.country == "us"
        assert not service.needs_questions(upload)


class TestSummary:
    def test_summary_lists_missing_and_changes_nothing(self, sites: dict[str, Site]) -> None:
        upload = _check(_create(batch(EGG, UG, CMD)).upload)
        assert upload.status == UploadStatus.CHECKED, upload.error
        summary = upload.summary or {}
        assert (summary["sites"], summary["known"], summary["new"]) == (3, 2, 1)
        assert summary["missing"] == [{"line": 3, "domain": "independent.co.ug"}]
        assert summary["country"] == ""
        assert summary["with_top_geo"] == 2
        assert summary["today"] == 0
        assert SiteMetric.objects.count() == 0
        assert TaskRun.objects.get(task_name="upload_check").status == TaskStatus.SUCCESS

    def test_deleted_site_is_not_in_base(self, sites: dict[str, Site]) -> None:
        Site.objects.filter(domain="commandlinux.com").update(is_deleted=True)
        summary = _check(_create(batch(EGG, CMD)).upload).summary or {}
        assert summary["known"] == 1
        assert [row["domain"] for row in summary["missing"]] == ["commandlinux.com"]


class TestWrite:
    def test_all_countries_writes_site_metrics(self, sites: dict[str, Site]) -> None:
        upload = _load(batch(EGG, UG, CMD))
        assert upload.site_list is None
        assert TaskRun.objects.get(task_name="upload_write").status == TaskStatus.SUCCESS
        assert (upload.result or {})["counts"]["measures_created"] == 2
        metric = SiteMetric.objects.get(site=sites["eggradients.com"])
        assert metric.source == MetricSource.AHREFS_BATCH
        assert metric.seller_id is None
        assert metric.checked_at == start_of_day(DAY)
        assert (metric.dr, metric.organic_traffic, metric.total_keywords) == (55, 99979, 16245)
        assert (metric.top_geo, metric.top_geo_traffic) == ("us", 53814)
        assert (metric.raw or {})["Ref. domains / All"] == "3709"
        assert SiteCountryMetric.objects.count() == 0
        # Домена не из базы нет — площадка не заводится.
        assert not Site.all_objects.filter(domain="independent.co.ug").exists()

    def test_country_writes_country_metrics_only(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG_US, all_countries=False), country="us")
        row = SiteCountryMetric.objects.get(site=sites["eggradients.com"])
        assert (row.country, row.organic_traffic, row.total_keywords) == ("us", 53814, 13175)
        assert row.source == MetricSource.AHREFS_BATCH
        assert row.checked_at == start_of_day(DAY)
        assert (row.raw or {})["Domain Rating"] == "55"
        # DR из выгрузки страны не пишется: он общий и лежит в сырых данных.
        assert SiteMetric.objects.count() == 0
        latest = SiteCountryLatest.objects.get(site=sites["eggradients.com"])
        assert (latest.country, latest.organic_traffic, latest.total_keywords) == (
            "us",
            53814,
            13175,
        )

    def test_same_file_again_opens_written_upload(self, sites: dict[str, Site]) -> None:
        first = _load(batch(EGG))
        again = _create(batch(EGG))
        assert again.duplicate
        assert again.upload.pk == first.pk
        assert SiteMetric.objects.count() == 1

    def test_same_day_other_file_updates_in_place(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG, CMD))
        fresher = ("eggradients.com/", "subdomains", "55", "16249", "99038", "(us, 53116)")
        upload = _check(_create(batch(fresher), name="second.csv").upload)
        assert (upload.summary or {})["today"] == 1
        upload = _write(upload)
        assert (upload.result or {})["counts"] == {
            "measures_created": 0,
            "measures_updated": 1,
            "measures_unchanged": 0,
        }
        assert SiteMetric.objects.count() == 2
        metric = SiteMetric.objects.get(site=sites["eggradients.com"])
        assert (metric.organic_traffic, metric.top_geo_traffic) == (99038, 53116)

    def test_new_day_adds_snapshot(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG_US, all_countries=False), country="us")
        _load(batch(EGG_US, all_countries=False), country="us", day=dt.date(2026, 10, 2))
        assert SiteCountryMetric.objects.count() == 2
        latest = SiteCountryLatest.objects.get(site=sites["eggradients.com"])
        assert latest.checked_at == start_of_day(dt.date(2026, 10, 2))


class TestLatestAfterBatch:
    """Пакетная запись должна обновлять и данные списка/карточки (ADR-065)."""

    def test_new_snapshot_updates_screen(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG, CMD))
        row = ProductSiteLatest.objects.get(site=sites["eggradients.com"])
        assert (row.dr, row.organic_traffic, row.total_keywords) == (55, 99979, 16245)
        assert (row.top_geo, row.top_geo_traffic) == ("us", 53814)
        assert row.metrics_at == start_of_day(DAY)
        other = ProductSiteLatest.objects.get(site=sites["commandlinux.com"])
        assert (other.dr, other.organic_traffic, other.total_keywords) == (59, 178262, 3966)

    def test_same_day_update_refreshes_screen(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG))
        fresher = ("eggradients.com/", "subdomains", "57", "17000", "100000", "(gb, 60000)")
        _load(batch(fresher))
        assert SiteMetric.objects.count() == 1
        row = ProductSiteLatest.objects.get(site=sites["eggradients.com"])
        assert (row.dr, row.organic_traffic, row.total_keywords) == (57, 100000, 17000)
        assert (row.top_geo, row.top_geo_traffic) == ("gb", 60000)

    def test_new_day_preserves_history_and_refreshes_screen(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG))
        tomorrow = dt.date(2026, 10, 2)
        fresher = ("eggradients.com/", "subdomains", "57", "17000", "100000", "(gb, 60000)")
        _load(batch(fresher), day=tomorrow)
        assert SiteMetric.objects.count() == 2
        assert SiteMetric.objects.get(checked_at=start_of_day(DAY)).organic_traffic == 99979
        row = ProductSiteLatest.objects.get(site=sites["eggradients.com"])
        assert (row.dr, row.organic_traffic, row.metrics_at) == (57, 100000, start_of_day(tomorrow))

    def test_unchanged_snapshot_repairs_stale_screen(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG))
        site = sites["eggradients.com"]
        SiteLatest.objects.filter(site=site).update(dr=None, organic_traffic=None)
        # Другой файл, те же метрики: повторная запись, а не открытие прежней загрузки.
        upload = _load(batch(EGG, UG))
        assert (upload.result or {})["counts"]["measures_unchanged"] == 1
        row = ProductSiteLatest.objects.get(site=site)
        assert (row.dr, row.organic_traffic) == (55, 99979)

    @pytest.mark.parametrize("seller_first", [True, False])
    def test_own_snapshot_wins_same_date_tie(
        self, sites: dict[str, Site], seller_first: bool
    ) -> None:
        def seller_snapshot() -> None:
            SiteMetric.objects.create(
                site=sites["eggradients.com"],
                seller=Seller.collaborator(),
                source=MetricSource.CSV_IMPORT,
                checked_at=start_of_day(DAY),
                dr=99,
                organic_traffic=1,
                total_keywords=1,
                top_geo="gb",
                top_geo_traffic=1,
            )

        if seller_first:
            seller_snapshot()
        _load(batch(EGG))
        if not seller_first:
            seller_snapshot()
        row = ProductSiteLatest.objects.get(site=sites["eggradients.com"])
        assert (row.dr, row.organic_traffic, row.total_keywords) == (55, 99979, 16245)
        assert (row.top_geo, row.top_geo_traffic) == ("us", 53814)
        assert row.metrics_seller is None
        assert review._metrics([sites["eggradients.com"].pk]) == {
            sites["eggradients.com"].pk: (55, 99979, None)
        }

    def test_refresh_only_affects_uploaded_sites(self, sites: dict[str, Site]) -> None:
        untouched = sites["commandlinux.com"]
        before = SiteLatest.objects.get(site=untouched).computed_at
        _load(batch(EGG))
        assert SiteLatest.objects.get(site=untouched).computed_at == before


class TestTopGeoInLatest:
    def test_catalog_without_geo_does_not_erase_top_geo(self, sites: dict[str, Site]) -> None:
        _load(batch(EGG))
        site = sites["eggradients.com"]
        # Позже — замер каталога Collaborator без гео: DR и трафик — его, топ-регион — прежний.
        SiteMetric.objects.create(
            site=site,
            dr=56,
            organic_traffic=90000,
            seller=Seller.collaborator(),
            source=MetricSource.CSV_IMPORT,
            checked_at=start_of_day(dt.date(2026, 10, 2)),
        )
        row = ProductSiteLatest.objects.get(site=site)
        assert (row.dr, row.organic_traffic) == (56, 90000)
        assert (row.top_geo, row.top_geo_traffic) == ("us", 53814)
        assert row.top_geo_at == start_of_day(DAY)


def test_price_list_needs_seller() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Upload.objects.create(
            kind=UploadKind.PRICE_LIST,
            prices_date=DAY,
            file_name="x.csv",
            file_path="x.csv",
            file_sha256="0" * 64,
        )


class TestScreens:
    """Путь через админку: форма → сводка → «Записать в базу» → итог (ADR-045)."""

    def _post(self, client: Client, content: bytes, **data: object) -> Any:
        file = SimpleUploadedFile("batch_analysis_2026-10-01.csv", content)
        payload = {"kind": "ahrefs_batch", "prices_date": DAY.isoformat(), "file": file, **data}
        return client.post(reverse("admin:sites_upload_add"), payload)

    def test_form_has_country_with_used_first(self, admin_client: Client) -> None:
        site = Site.objects.create(domain="a.com")
        SiteCountryMetric.objects.create(site=site, country="gb", organic_traffic=1)
        page = admin_client.get(reverse("admin:sites_upload_add")).content.decode()
        assert "Ahrefs Batch Analysis" in page
        assert "Страна выгрузки" in page
        assert 'data-search="все страны all locations" data-flag="globe">' in page
        assert 'data-flag="us">США · US</option>' in page
        assert "flags/sprite-hq.css" in page
        used = page.index('optgroup label="Уже загружали"')
        assert page.index("Великобритания · GB", used) < page.index("Все страны по алфавиту")
        assert 'data-search="us сша соединенные штаты америки' in page

    def test_country_path_to_result(self, admin_client: Client, sites: dict[str, Site]) -> None:
        response = self._post(admin_client, batch(EGG_US, all_countries=False), country="us")
        upload = Upload.objects.get()
        assert (upload.seller_id, upload.country) == (None, "us")
        summary_url = reverse("admin:sites_upload_summary", args=[upload.pk])
        assert response["Location"] == summary_url
        page = admin_client.get(summary_url).content.decode()
        assert 'flag-sprite flag-u flag-_s" aria-hidden="true"></span><b>США</b>' in page
        assert "Записать в базу" in page
        admin_client.post(reverse("admin:sites_upload_write", args=[upload.pk]))
        upload.refresh_from_db()
        assert upload.status == UploadStatus.DONE, upload.error
        page = admin_client.get(summary_url).content.decode()
        assert "новых замеров" in page
        assert "?region=us" in page
        assert SiteCountryMetric.objects.filter(country="us").count() == 1
        listing = admin_client.get(reverse("admin:sites_upload_changelist")).content.decode()
        assert "Ahrefs<div" in listing and "США" in listing

    def test_unknown_country_rejected_by_form(self, admin_client: Client) -> None:
        response = self._post(admin_client, batch(EGG_US, all_countries=False), country="zz")
        assert response.status_code == 200
        assert not Upload.objects.exists()

    def test_country_ignored_for_price_list(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="LinkHub Media", currency="EUR")
        self._post(
            admin_client,
            b"Website,GP Price\nknown.com,150\n",
            kind="price_list",
            seller=seller.pk,
            country="us",
        )
        assert Upload.objects.get().country is None


def test_country_names_and_flags() -> None:
    """Короткие имена вместо родительного падежа пакета; флаг — кусок общей картинки."""
    from apps.sites import countries

    assert countries.name("mm") == "Мьянма"
    assert countries.name("nc") == "Новая Каледония"
    assert countries.name("zz") == "ZZ"
    assert countries.flag_code("GB") == "gb"
    assert countries.flag_code("zz") is None
    assert str(countries.flag_html("us")) == (
        '<span class="seo-flag flag-sprite flag-u flag-_s" aria-hidden="true"></span>'
    )
    assert "seo-flag-globe" in countries.flag_html(countries.GLOBE)
    assert countries.flag_html(None) == ""
    united = [c.code for c in countries.all_countries() if "united" in c.search]
    assert {"us", "gb", "ae"} <= set(united)
