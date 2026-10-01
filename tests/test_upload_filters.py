"""Фильтры каталога и две кнопки записи: «Обновить в базе», «Добавить новые» (E1-08, ADR-044)."""

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.sites.models import (
    MetricSource,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteListItem,
    SiteMetric,
    SitePrice,
    SiteStatus,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import catalog, filters, service
from apps.sites.uploads.files import read_table
from apps.sites.uploads.plan import start_of_day

PRICE_DATE = dt.date(2026, 10, 1)
HEADERS = [*catalog.REQUIRED, "Country", "Monthly Traffic"]


def _row(domain: str, **values: str) -> dict[str, str]:
    base = {
        "Domain": f"https://www.{domain}",
        "Collaborator URL": f"https://collaborator.pro/{domain}",
        "Category": "Internet",
        "Website languages": "English",
        "Website type": "Personal blog",
        "Link type article": "dofollow",
        "Advertising mark article": "No",
        "Number of links article": "1",
        "Publishing price article, EUR": "100.00",
        "DR": "40",
        "Organic traffic": "5000",
        "Keywords": "200",
        "Country": "USA",
        "Monthly Traffic": "9000",
    }
    return {**base, **values}


def _csv(rows: list[dict[str, str]]) -> str:
    lines = [";".join(f'"{h}"' for h in HEADERS)]
    lines += [";".join(f'"{row.get(h, "")}"' for h in HEADERS) for row in rows]
    return "\n".join(lines) + "\n"


ROWS = [
    _row("known.com", DR="20"),
    _row("usa-strong.com", DR="55"),
    _row("usa-weak.com", DR="15"),
    _row("india.com", DR="60", Country="India", **{"Website languages": "English, Hindi"}),
    _row("ukraine.com", DR="50", Country="Ukraine, Poland", **{"Website languages": "Ukrainian"}),
    _row("nofollow.com", DR="70", **{"Link type article": "nofollow"}),
    _row("pricey.com", DR="65", **{"Publishing price article, EUR": "600.00"}),
    _row("nodr.com", DR=""),
]


class TestSpec:
    def test_parse_keeps_known_non_empty(self) -> None:
        spec = filters.parse_spec(
            {
                "country": [" USA ", "India", "USA", ""],
                "dr": {"min": "30", "max": ""},
                "price": {"max": "1 200,5"},
                "languages": [],
                "zone": [".com"],
                "keywords": {"min": "abc"},
            }
        )
        assert spec == {
            "country": ["USA", "India"],
            "dr": {"min": 30.0, "max": None},
            "price": {"min": None, "max": 1200.5},
        }

    def test_parse_garbage(self) -> None:
        assert filters.parse_spec("x") == {}
        assert filters.parse_spec({"dr": 5}) == {}

    def test_describe(self) -> None:
        spec = filters.parse_spec(
            {
                "dr": {"min": 30},
                "country": ["USA", "India"],
                "price": {"min": 50, "max": 150},
                "link_type": ["dofollow"],
            }
        )
        assert filters.describe(spec) == (
            "страна: USA, India · DR ≥ 30 · цена публикации €50–€150 · тип ссылки: dofollow"
        )
        assert filters.describe({}) == "без фильтра"


class TestMatches:
    @pytest.fixture
    def rows(self, tmp_path: Path) -> dict[str, tuple[Any, ...]]:
        path = tmp_path / "c.csv"
        path.write_text(_csv([*ROWS, _row("known.com", **{"Publishing price article, EUR": "50"})]))
        return dict(filters.rows_from_table(read_table(path)))

    def _pass(self, rows: dict[str, tuple[Any, ...]], data: dict[str, Any]) -> set[str]:
        spec = filters.parse_spec(data)
        return {domain for domain, row in rows.items() if filters.matches(row, spec)}

    def test_empty_filter_passes_all(self, rows: dict[str, tuple[Any, ...]]) -> None:
        assert len(self._pass(rows, {})) == len(ROWS)

    def test_duplicate_domain_takes_lower_price(self, rows: dict[str, tuple[Any, ...]]) -> None:
        price = [f.key for f in filters.FIELDS].index("price")
        assert rows["known.com"][price] == 50.0

    def test_any_of_several_values_case_insensitive(self, rows: dict[str, tuple[Any, ...]]) -> None:
        found = self._pass(rows, {"country": ["india", "Poland"]})
        assert found == {"india.com", "ukraine.com"}
        assert self._pass(rows, {"languages": ["Hindi"]}) == {"india.com"}

    def test_range_and_missing_value(self, rows: dict[str, tuple[Any, ...]]) -> None:
        assert self._pass(rows, {"dr": {"min": 50, "max": 65}}) == {
            "usa-strong.com",
            "india.com",
            "ukraine.com",
            "pricey.com",
        }
        # Без DR при заданном диапазоне — не проходит.
        assert "nodr.com" not in self._pass(rows, {"dr": {"max": 100}})

    def test_combined(self, rows: dict[str, tuple[Any, ...]]) -> None:
        data = {
            "country": ["USA"],
            "dr": {"min": 30},
            "link_type": ["dofollow"],
            "price": {"max": 500},
        }
        assert self._pass(rows, data) == {"usa-strong.com"}


@pytest.mark.django_db
class TestSuggestions:
    def test_recent_first_unique_limited(self) -> None:
        seller = Seller.collaborator()

        def upload(runs: list[dict[str, Any]]) -> Upload:
            return Upload.objects.create(
                kind=UploadKind.COLLABORATOR_CATALOG,
                seller=seller,
                prices_date=PRICE_DATE,
                file_name="c.csv",
                file_path="x",
                file_sha256="0" * 64,
                result={"runs": runs},
            )

        newer = upload([{"action": "new", "filters": {"country": ["India"], "dr": {"min": 40}}}])
        older = upload(
            [
                {"action": "new", "filters": {"country": ["USA", "India"], "dr": {"min": 30}}},
                {"action": "known", "filters": {}},
            ]
        )
        chips = filters.suggestions([newer, older])
        assert [c["text"] for c in chips["country"]] == ["India", "USA"]
        assert [c["text"] for c in chips["dr"]] == ["от 40", "от 30"]
        assert chips["languages"] == []


@pytest.fixture
def uploads_dir(settings: Any, tmp_path: Path) -> None:
    settings.UPLOADS_DIR = tmp_path / "uploads"


@pytest.fixture
def base(uploads_dir: None) -> Site:
    Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="known.com", topics=["Старое"])
    price = SitePrice.objects.create(
        site=site,
        seller=Seller.collaborator(),
        placement_cents=9000,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 9, 27)),
        reviewed_at=timezone.now(),
    )
    site.price = price
    site.save(update_fields=["price"])
    return site


def _catalog_upload() -> Upload:
    file = SimpleUploadedFile("collaborator.csv", _csv(ROWS).encode("utf-8-sig"))
    upload = service.create_upload(
        file,
        kind=UploadKind.COLLABORATOR_CATALOG,
        seller=Seller.collaborator(),
        prices_date=PRICE_DATE,
    ).upload
    service.start(upload, UploadStatus.CHECKING)
    upload_check.delay(upload.pk)
    upload.refresh_from_db()
    return upload


def _run(upload: Upload, action: str, parts: list[str] | None = None, spec: Any = None) -> Upload:
    service.start(upload, UploadStatus.WRITING)
    upload_write.delay(upload.pk, action, parts, spec)
    upload.refresh_from_db()
    assert upload.status == UploadStatus.DONE, upload.error
    return upload


@pytest.mark.django_db
class TestRuns:
    def test_known_updates_only_known(self, base: Site) -> None:
        upload = _catalog_upload()
        assert upload.summary is not None
        assert (upload.summary["known"], upload.summary["new"]) == (1, len(ROWS) - 1)
        upload = _run(upload, "known", ["prices", "metrics", "card"])
        assert Site.objects.count() == 1
        base.refresh_from_db()
        assert base.topics == ["Интернет"]
        assert base.price is not None and base.price.placement_cents == 10000
        assert upload.site_list is not None
        assert list(
            SiteListItem.objects.filter(site_list=upload.site_list).values_list(
                "site__domain", flat=True
            )
        ) == ["known.com"]
        (run,) = (upload.result or {})["runs"]
        assert (run["action"], run["sites"], run["filter_text"]) == ("known", 1, "")

    def test_known_without_metrics_and_card(self, base: Site) -> None:
        upload = _run(_catalog_upload(), "known", ["prices"])
        base.refresh_from_db()
        assert base.topics == ["Старое"]
        assert not SiteMetric.objects.filter(site=base).exists()
        assert base.price is not None and base.price.placement_cents == 10000
        assert (upload.result or {})["runs"][0]["parts"] == ["prices"]

    def test_new_with_filter_then_another_filter(self, base: Site) -> None:
        upload = _catalog_upload()
        spec = {
            "country": ["USA"],
            "dr": {"min": 30},
            "link_type": ["dofollow"],
            "price": {"max": 500},
        }
        upload = _run(upload, "new", ["prices", "metrics", "card"], spec)
        assert set(Site.objects.values_list("domain", flat=True)) == {"known.com", "usa-strong.com"}
        # Не подошедшие не записаны и не отклонены — их просто нет.
        assert not ProductSite.objects.filter(status=SiteStatus.REJECTED).exists()
        assert upload.summary is not None
        assert upload.summary["new"] == len(ROWS) - 2  # сводка после записи

        upload = _run(upload, "new", ["prices", "metrics", "card"], {"country": ["India", "USA"]})
        added = set(Site.objects.values_list("domain", flat=True)) - {"known.com"}
        assert added == {
            "usa-strong.com",
            "usa-weak.com",
            "india.com",
            "nofollow.com",
            "pricey.com",
            "nodr.com",
        }
        runs = (upload.result or {})["runs"]
        assert [r["sites"] for r in runs] == [1, 5]  # usa-strong.com второй раз не добавлен
        assert runs[1]["filter_text"] == "страна: India, USA"
        assert filters.count_new(upload, {}).total == 1  # остался ukraine.com

    def test_new_without_card(self, base: Site) -> None:
        _run(_catalog_upload(), "new", ["prices"], {"dr": {"min": 55, "max": 55}})
        site = Site.objects.get(domain="usa-strong.com")
        assert (site.source, site.topics, site.language) == ("Collaborator", None, None)
        assert site.price is not None
        assert not SiteMetric.objects.filter(site=site).exists()

    def test_count_new(self, base: Site) -> None:
        upload = _catalog_upload()
        count = filters.count_new(upload, filters.parse_spec({"country": ["USA"]}))
        assert (count.passed, count.total) == (5, 7)
        choices = dict(filters.choices(upload)["country"])
        assert choices == {"USA": 5, "India": 1, "Ukraine": 1, "Poland": 1}


@pytest.mark.django_db
class TestScreen:
    def test_actions_page_count_and_buttons(self, admin_client: Client, base: Site) -> None:
        upload = _catalog_upload()
        url = reverse("admin:sites_upload_summary", args=[upload.pk])
        page = admin_client.get(url).content.decode()
        assert "Обновить в базе" in page and "Добавить новые" in page
        assert 'id="seo-filter-data"' in page

        response = admin_client.post(
            reverse("admin:sites_upload_count", args=[upload.pk]),
            data=json.dumps({"filters": {"dr": {"min": 50}}}),
            content_type="application/json",
        )
        assert response.json() == {"passed": 5, "total": 7, "text": "DR ≥ 50"}

        admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]),
            {
                "action": "new",
                "parts": ["prices", "metrics", "card"],
                "filters": json.dumps({"dr": {"min": 60}}),
            },
        )
        upload.refresh_from_db()
        assert upload.status == UploadStatus.DONE
        assert set(Site.objects.values_list("domain", flat=True)) == {
            "known.com",
            "india.com",
            "nofollow.com",
            "pricey.com",
        }
        # После записи каталог открывает те же кнопки, с историей и облачками.
        page = admin_client.get(url).content.decode()
        assert "Что уже записано из этого файла" in page and "DR ≥ 60" in page
        data = re.search(
            r'<script id="seo-filter-data" type="application/json">(.*?)</script>', page
        )
        assert data is not None
        fields = {field["key"]: field for field in json.loads(data.group(1))}
        assert [chip["text"] for chip in fields["dr"]["chips"]] == ["от 60"]

    def test_known_needs_parts(self, admin_client: Client, base: Site) -> None:
        upload = _catalog_upload()
        admin_client.post(
            reverse("admin:sites_upload_write", args=[upload.pk]), {"action": "known"}
        )
        upload.refresh_from_db()
        assert upload.status == UploadStatus.CHECKED

    def test_catalog_refuses_write_all(self, admin_client: Client, base: Site) -> None:
        upload = _catalog_upload()
        admin_client.post(reverse("admin:sites_upload_write", args=[upload.pk]), {"action": "all"})
        upload.refresh_from_db()
        assert upload.status == UploadStatus.CHECKED


@pytest.mark.django_db
def test_recheck(admin_client: Client, base: Site) -> None:
    upload = _catalog_upload()
    Upload.objects.filter(pk=upload.pk).update(summary={"rows": 0})
    admin_client.post(reverse("admin:sites_upload_recheck", args=[upload.pk]))
    upload.refresh_from_db()
    assert upload.status == UploadStatus.CHECKED
    assert upload.summary is not None and upload.summary["known"] == 1
