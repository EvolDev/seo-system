"""«Площадки»: топ-регион и выбор региона (E1-10, ADR-045)."""

import datetime as dt

import pytest
from django.contrib.admin.templatetags.admin_list import result_headers
from django.test import Client
from django.urls import reverse

from apps.sites.models import (
    MetricSource,
    Product,
    Site,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
)
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

URL = reverse("admin:sites_productsitelatest_changelist")
DAY = start_of_day(dt.date(2026, 10, 1))


@pytest.fixture
def base() -> dict[str, Site]:
    """egg — выгрузки «все страны» и по США; podchaser — только «все страны»; plain — ничего."""
    Product.objects.create(name="Convertio", domain="convertio.co")
    sites = {name: Site.objects.create(domain=f"{name}.com") for name in ("egg", "pod", "plain")}
    batch = MetricSource.AHREFS_BATCH
    SiteMetric.objects.create(
        site=sites["egg"],
        dr=55,
        organic_traffic=99979,
        top_geo="us",
        top_geo_traffic=53814,
        source=batch,
        checked_at=DAY,
    )
    SiteMetric.objects.create(
        site=sites["pod"],
        dr=85,
        organic_traffic=1197709,
        top_geo="in",
        top_geo_traffic=924604,
        source=batch,
        checked_at=DAY,
    )
    SiteCountryMetric.objects.create(
        site=sites["egg"],
        country="us",
        organic_traffic=53814,
        total_keywords=13175,
        source=batch,
        checked_at=DAY,
    )
    return sites


def test_top_geo_column_without_region(admin_client: Client, base: dict[str, Site]) -> None:
    response = admin_client.get(URL, {"list": "all"})
    page = response.content.decode()
    assert "топ регион" in page.lower()
    assert 'flag-sprite flag-u flag-_s" aria-hidden="true"></span>US 53814' in page
    assert "flags/sprite-hq.css" in page
    assert "IN 924604" in page
    assert "Индия: больше всего трафика; замер 01.10.2026" in page
    assert "трафик US" not in page


def test_region_choices_only_from_country_exports(
    admin_client: Client, base: dict[str, Site]
) -> None:
    response = admin_client.get(URL, {"list": "all"})
    region = next(s for s in response.context["cl"].filter_specs if s.title == "регион")
    # Индия — только топ-регион выгрузки «все страны»: в выборе её нет.
    assert region.lookup_choices == [("us", "США · US (1)")]
    # Выбор — со флагами и поиском поверх списка (seo/country-picker.js).
    page = response.content.decode()
    assert "data-country-picker data-navigate" in page
    assert 'data-flag="us" data-search="us сша' in page
    assert 'data-flag="globe" data-search="все all">Все</option>' in page
    assert "seo/country-picker.js" in page


def test_region_columns_and_empty_without_data(admin_client: Client, base: dict[str, Site]) -> None:
    response = admin_client.get(URL, {"list": "all", "region": "us"})
    page = response.content.decode()
    assert "трафик US" in page and "ключи US" in page
    rows = {row.domain: row for row in response.context["cl"].result_list}
    assert (rows["egg.com"].region_traffic, rows["egg.com"].region_keywords) == (53814, 13175)
    assert rows["pod.com"].region_traffic is None
    assert rows["plain.com"].region_traffic is None
    assert 'title="Ahrefs, замер 01.10.2026">13175' in page
    # Топ-регион виден и при выбранном регионе.
    assert "IN 924604" in page


def test_region_not_loaded_is_empty(admin_client: Client, base: dict[str, Site]) -> None:
    response = admin_client.get(URL, {"list": "all", "region": "gb"})
    assert "трафик GB" in response.content.decode()
    assert all(row.region_traffic is None for row in response.context["cl"].result_list)


def test_region_range_filter_and_sorting(admin_client: Client, base: dict[str, Site]) -> None:
    response = admin_client.get(
        URL, {"list": "all", "region": "us", "region_traffic__range__gte": "1000"}
    )
    assert [row.domain for row in response.context["cl"].result_list] == ["egg.com"]
    titles = [spec.title for spec in response.context["cl"].filter_specs]
    assert titles.index("трафик US") == titles.index("трафик") + 1
    assert "ключи US" in titles


def test_region_choice_resets_its_ranges_and_sorting(
    admin_client: Client, base: dict[str, Site]
) -> None:
    response = admin_client.get(
        URL, {"list": "all", "region": "us", "region_traffic__range__gte": "1", "o": "5"}
    )
    region = next(s for s in response.context["cl"].filter_specs if s.title == "регион")
    every = next(iter(region.choices(response.context["cl"])))
    assert every["display"] == "Все"
    assert "region" not in every["query_string"]
    assert "o=" not in every["query_string"]


def test_ahrefs_link_for_chosen_list(admin_client: Client, base: dict[str, Site]) -> None:
    site_list = SiteList.objects.create(name="Collaborator · 02.10.2026")
    SiteListItem.objects.create(site_list=site_list, site=base["egg"])
    link = reverse("admin:sites_sitelist_ahrefs", args=[site_list.pk])
    # По умолчанию — все площадки, и ссылка ни на один список не ведёт (E1-19).
    assert link not in admin_client.get(URL).content.decode()
    assert link in admin_client.get(URL, {"list": str(site_list.pk)}).content.decode()


@pytest.mark.parametrize("country", ["us", "gb"])
def test_region_traffic_header_sorts_selected_country(
    admin_client: Client, base: dict[str, Site], country: str
) -> None:
    SiteCountryMetric.objects.create(
        site=base["pod"],
        country="us",
        organic_traffic=90000,
        source=MetricSource.AHREFS_BATCH,
        checked_at=DAY,
    )
    for site, traffic in ((base["egg"], 800), (base["pod"], 200)):
        SiteCountryMetric.objects.create(
            site=site,
            country="gb",
            organic_traffic=traffic,
            source=MetricSource.AHREFS_BATCH,
            checked_at=DAY,
        )
    response = admin_client.get(URL, {"list": "all", "region": country})
    header = next(
        item
        for item in result_headers(response.context["cl"])
        if item["text"] == f"трафик {country.upper()}"
    )
    assert header["sortable"]
    ascending_url = header["url_primary"]
    assert isinstance(ascending_url, str)
    ascending = admin_client.get(URL + ascending_url)
    rows = [row.domain for row in ascending.context["cl"].result_list]
    expected = ["egg.com", "pod.com"] if country == "us" else ["pod.com", "egg.com"]
    assert rows == [*expected, "plain.com"]
    selected = next(
        item
        for item in result_headers(ascending.context["cl"])
        if item["text"] == f"трафик {country.upper()}"
    )
    descending_url = selected["url_primary"]
    assert isinstance(descending_url, str)
    descending = admin_client.get(URL + descending_url)
    assert [row.domain for row in descending.context["cl"].result_list] == [
        "plain.com",
        *reversed(expected),
    ]
    without_region = admin_client.get(URL, {"list": "all"})
    assert not any(
        item["text"] == f"трафик {country.upper()}"
        for item in result_headers(without_region.context["cl"])
    )
