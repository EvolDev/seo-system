"""Страна площадки (ADR-082): каталог продавца, карточка, фильтр «Площадок».

Справочник названий — `countries.find`; разметка, сводка и запись прайса — через
сервис загрузки, как в `test_upload_write.py`; отмена — `undo.undo`; фильтр —
по экрану «Площадки», как в `test_product_sites_admin.py`.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.sites import countries
from apps.sites.admin import SiteCountryFilter
from apps.sites.models import (
    ExchangeRate,
    Product,
    Seller,
    Site,
    Upload,
    UploadKind,
    UploadStatus,
)
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import service, undo
from apps.sites.uploads.columns import Field

pytestmark = pytest.mark.django_db

PRICE_DATE = dt.date(2026, 11, 16)
LIST_URL = reverse("admin:sites_productsitelatest_changelist")

# Все 42 названия из колонки `country` каталога sites.xlsx (16.11.2026).
SITES_XLSX = {
    "Brazil": "br", "Netherlands": "nl", "Italy": "it", "United States": "us",
    "Germany": "de", "Spain": "es", "Portugal": "pt", "France": "fr",
    "United Kingdom": "gb", "Argentina": "ar", "Mexico": "mx", "Canada": "ca",
    "Turkey": "tr", "Indonesia": "id", "Colombia": "co", "Romania": "ro",
    "Australia": "au", "Poland": "pl", "India": "in", "Chile": "cl",
    "Venezuela": "ve", "Peru": "pe", "Panama": "pa", "Ecuador": "ec",
    "Guatemala": "gt", "Austria": "at", "Bulgaria": "bg", "Norway": "no",
    "Finland": "fi", "Uruguay": "uy", "Belgium": "be", "Dominican Republic": "do",
    "Ukraine": "ua", "Morocco": "ma", "Sweden": "se", "Bolivia": "bo",
    "Nigeria": "ng", "Greece": "gr", "Denmark": "dk", "South Korea": "kr",
    "Cuba": "cu", "Switzerland": "ch",
}  # fmt: skip


# ---------- Справочник: название целиком → код ----------


def test_every_country_of_sites_xlsx_is_found() -> None:
    assert {name: countries.find(name) for name in SITES_XLSX} == SITES_XLSX


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Бразилия", "br"),
        ("BR", "br"),
        ("  brazil ", "br"),
        ("Португалия", "pt"),
        ("USA", "us"),
        ("США", "us"),
        ("UK", "gb"),
        ("Türkiye", "tr"),
        ("Турция", "tr"),
    ],
)
def test_other_spellings(text: str, code: str) -> None:
    assert countries.find(text) == code


@pytest.mark.parametrize("text", ["", None, "Global", "Europe", "LATAM", "United", "Arab Emirates"])
def test_not_a_country(text: str | None) -> None:
    # Слова по отдельности не ищем: «United» — не США, «Arab Emirates» — не ОАЭ.
    assert countries.find(text) is None


def test_search_words_did_not_change() -> None:
    # Синонимы стали кортежами, а поиск в выборе страны ищет по тем же словам.
    usa = countries.get("us")
    assert usa is not None
    assert {"usa", "америка", "united", "states"} <= set(usa.search.split())


# ---------- Загрузка каталога продавца ----------


@pytest.fixture(autouse=True)
def uploads_dir(settings: object, tmp_path: Path) -> Path:
    settings.UPLOADS_DIR = tmp_path / "uploads"  # type: ignore[attr-defined]
    return tmp_path / "uploads"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def seller() -> Seller:
    ExchangeRate.objects.create(currency="USD", rate_date=PRICE_DATE, rate=Decimal("1.16"))
    return Seller.objects.create(name="Sites Catalog", currency="USD")


CATALOG = (
    "url,price_usd,country\n"
    "https://brasil.com.br/,$100,Brazil\n"
    "portugal.pt,$100,Portugal\n"
    "switch.com,$100,Portugal\n"
    "empty.com,$100,Brazil\n"
    "keep.com,$100,\n"
    "atlantis.com,$100,Atlantis\n"
    "atlantis2.com,$100,Atlantis\n"
)


@pytest.fixture
def base(convertio: Product) -> None:
    Site.objects.create(domain="switch.com", country="br")
    Site.objects.create(domain="empty.com")
    Site.objects.create(domain="keep.com", country="de")


def _upload(seller: Seller, content: str = CATALOG) -> Upload:
    file = SimpleUploadedFile("sites.csv", content.encode("utf-8"))
    upload = service.create_upload(
        file, kind=UploadKind.PRICE_LIST, seller=seller, prices_date=PRICE_DATE
    ).upload
    if service.needs_questions(upload):
        assert service.confirm_mapping(upload, upload.mapping or {}, "USD") == []
    return upload


def _check(upload: Upload) -> Upload:
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


def _countries() -> dict[str, str | None]:
    return dict(Site.all_objects.values_list("domain", "country"))


class TestPriceList:
    def test_country_column_is_guessed(self, seller: Seller) -> None:
        upload = _upload(seller)
        assert upload.mapping is not None
        assert upload.mapping["country"] == Field.COUNTRY

    @pytest.mark.usefixtures("base")
    def test_summary_before_write(self, seller: Seller) -> None:
        summary = _check(_upload(seller)).summary
        assert summary is not None
        assert summary["countries"] == [
            {"code": "br", "name": "Бразилия", "count": 2},
            {"code": "pt", "name": "Португалия", "count": 2},
        ]
        assert summary["country_filled"] == 1  # empty.com
        assert summary["country_changed"] == [
            {"domain": "switch.com", "old": "Бразилия", "new": "Португалия"}
        ]
        assert summary["country_unknown"] == [{"value": "Atlantis", "count": 2}]
        # Сводка базу не меняет.
        assert _countries()["switch.com"] == "br"

    @pytest.mark.usefixtures("base")
    def test_write_latest_upload_wins(self, seller: Seller) -> None:
        _write(_check(_upload(seller)))
        assert _countries() == {
            "brasil.com.br": "br",  # новая площадка — сразу со страной
            "portugal.pt": "pt",
            "switch.com": "pt",  # была Бразилия — пишет последняя загрузка
            "empty.com": "br",
            "keep.com": "de",  # пустая ячейка страну не стирает
            "atlantis.com": None,  # не узнали — страна не пишется, площадка есть
            "atlantis2.com": None,
        }

    @pytest.mark.usefixtures("base")
    def test_undo_brings_countries_back(self, seller: Seller) -> None:
        upload = _write(_check(_upload(seller)))
        undo.undo(upload)
        assert _countries() == {"switch.com": "br", "empty.com": None, "keep.com": "de"}

    def test_deleted_site_comes_back_with_country(self, seller: Seller) -> None:
        Site.all_objects.create(domain="portugal.pt", is_deleted=True)
        _write(_check(_upload(seller, "url,price_usd,country\nportugal.pt,$100,Portugal\n")))
        site = Site.objects.get(domain="portugal.pt")
        assert site.country == "pt"


# ---------- Фильтр «Площадок» ----------


def _list(client: Client, **params: str | list[str]) -> ChangeList:
    response = client.get(LIST_URL, {"list": "all", **params})
    assert response.status_code == 200
    changelist: ChangeList = response.context["cl"]
    return changelist


def _domains(client: Client, **params: str | list[str]) -> set[str]:
    return {row.domain for row in _list(client, **params).result_list}


def _choices(client: Client, **params: str | list[str]) -> list[str]:
    changelist = _list(client, **params)
    spec = next(s for s in changelist.filter_specs if isinstance(s, SiteCountryFilter))
    return [str(choice["display"]) for choice in spec.choices(changelist)]


class TestFilter:
    @pytest.fixture(autouse=True)
    def sites(self, convertio: Product) -> None:
        Site.objects.create(domain="brasil.com.br", country="br", language="pt")
        Site.objects.create(domain="noticias.com.br", country="br", language="pt")
        Site.objects.create(domain="publico.pt", country="pt", language="pt")
        Site.objects.create(domain="spiegel.de", country="de", language="de")
        Site.objects.create(domain="unknown.com", language="en")

    def test_all_by_default(self, admin_client: Client) -> None:
        assert len(_domains(admin_client)) == 5

    def test_brazil_and_portugal(self, admin_client: Client) -> None:
        found = _domains(admin_client, country=["br", "pt"])
        assert found == {"brasil.com.br", "noticias.com.br", "publico.pt"}

    def test_unchecking_one_keeps_unknown(self, admin_client: Client) -> None:
        # Сняли Германию из «отмечены все» — площадки без страны остаются.
        found = _domains(admin_client, country=["br", "pt", "none"])
        assert found == {"brasil.com.br", "noticias.com.br", "publico.pt", "unknown.com"}

    def test_only_unknown(self, admin_client: Client) -> None:
        assert _domains(admin_client, country="none") == {"unknown.com"}

    def test_counts_follow_other_filters(self, admin_client: Client) -> None:
        assert _choices(admin_client) == [
            "Все",
            "Бразилия · BR (2)",
            "Германия · DE (1)",
            "Португалия · PT (1)",
            "Не указана (1)",
        ]
        # Другой фильтр сужает и числа: немецких площадок нет — пункта Германии нет.
        assert _choices(admin_client, language="pt") == [
            "Все",
            "Бразилия · BR (2)",
            "Португалия · PT (1)",
        ]


# ---------- Карточка и «Факты о площадке» ----------


class TestCard:
    def test_card_shows_country_picker(self, admin_client: Client, convertio: Product) -> None:
        site = Site.objects.create(domain="publico.pt", country="pt")
        page = admin_client.get(reverse("admin:sites_site_card", args=[site.pk])).content.decode()
        assert 'name="country" data-site-country' in page
        assert '<option value="pt" selected' in page

    @pytest.mark.parametrize(("posted", "stored"), [("br", "br"), ("PT", "pt"), ("", None)])
    def test_card_sets_country(
        self, admin_client: Client, convertio: Product, posted: str, stored: str | None
    ) -> None:
        site = Site.objects.create(domain="site.com", country="de")
        url = reverse("admin:sites_site_card_country", args=[site.pk])
        response = admin_client.post(url, {"country": posted}, HTTP_X_SEO_PARTIAL="1")
        assert response.status_code in (200, 302)
        site.refresh_from_db()
        assert site.country == stored

    def test_card_rejects_unknown_country(self, admin_client: Client) -> None:
        site = Site.objects.create(domain="site.com")
        url = reverse("admin:sites_site_card_country", args=[site.pk])
        assert admin_client.post(url, {"country": "zz"}).status_code == 400
        assert admin_client.get(url).status_code == 405

    def test_readonly_user_cannot_set(self, client: Client, django_user_model: type[User]) -> None:
        site = Site.objects.create(domain="site.com")
        client.force_login(django_user_model.objects.create_user("viewer", is_staff=True))
        url = reverse("admin:sites_site_card_country", args=[site.pk])
        assert client.post(url, {"country": "br"}).status_code == 403
        site.refresh_from_db()
        assert site.country is None

    def test_facts_form_is_a_country_choice(self, admin_client: Client) -> None:
        site = Site.objects.create(domain="site.com", country="br")
        response = admin_client.get(reverse("admin:sites_site_change", args=[site.pk]))
        form = response.context["adminform"].form
        assert form.fields["country"].choices[0] == ("", "не указана")
        assert ("pt", "Португалия · PT") in form.fields["country"].choices
