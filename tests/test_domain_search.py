"""Поиск столбиком доменов в «Размещениях», «Площадках» и «Каталоге площадок».

Вставили в поиск несколько доменов — в списке строки ровно этих доменов, а
рядом со счётчиком — домены вставки, которых в списке нет. Одно слово ищется
как раньше.
"""

from typing import Any

import pytest
from django.contrib.admin.views.main import ChangeList
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement
from apps.sites.models import Product, Site

pytestmark = pytest.mark.django_db

PLACEMENTS = reverse("admin:placements_placement_changelist")
SITES = reverse("admin:sites_productsitelatest_changelist")
CATALOG = reverse("admin:sites_site_changelist")
KEYWORDS = reverse("admin:keywords_keyword_changelist")

# Как из таблицы: с www, адресом статьи и переводом строки в конце.
PASTED = "www.lensa.com\r\nhttps://veed.io/blog/post\r\ntrueconf.com\r\n"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def placements(convertio: Product) -> dict[str, Placement]:
    names = ("lensa.com", "valensa.com", "veed.io", "amasty.com")
    return {
        name: Placement.objects.create(site=Site.objects.create(domain=name), product=convertio)
        for name in names
    }


def _get(client: Client, url: str, **params: str) -> Any:
    response = client.get(url, params)
    assert response.status_code == 200
    return response


def _domains(response: Any) -> set[str]:
    changelist: ChangeList = response.context["cl"]
    return {
        row.domain if hasattr(row, "domain") else row.site.domain for row in changelist.result_list
    }


@pytest.mark.usefixtures("placements")
class TestPlacements:
    def test_column_finds_exactly_these_domains(self, admin_client: Client) -> None:
        # valensa.com тоже содержит «lensa.com», но домен сравнивается целиком.
        assert _domains(_get(admin_client, PLACEMENTS, q=PASTED)) == {"lensa.com", "veed.io"}

    def test_missing_domains_are_listed(self, admin_client: Client) -> None:
        page = _get(admin_client, PLACEMENTS, q=PASTED).content.decode()
        assert "нет в списке (1): trueconf.com" in page
        assert 'data-copy="trueconf.com"' in page

    def test_all_found_says_nothing(self, admin_client: Client) -> None:
        page = _get(admin_client, PLACEMENTS, q="lensa.com\nveed.io").content.decode()
        assert "нет в списке" not in page

    def test_one_word_searches_as_before(self, admin_client: Client) -> None:
        response = _get(admin_client, PLACEMENTS, q="lensa")
        assert _domains(response) == {"lensa.com", "valensa.com"}
        assert "нет в списке" not in response.content.decode()

    def test_field_keeps_the_column(self, admin_client: Client) -> None:
        page = _get(admin_client, PLACEMENTS, q=PASTED).content.decode()
        assert 'rows="3"' in page
        assert "www.lensa.com\r\nhttps://veed.io/blog/post\r\ntrueconf.com</textarea>" in page

    def test_other_placements_of_a_found_site_stay_one_row(
        self, admin_client: Client, placements: dict[str, Placement], convertio: Product
    ) -> None:
        # Повтор на той же площадке — строка по-прежнему одна (ADR-073).
        Placement.objects.create(site=placements["lensa.com"].site, product=convertio)
        changelist = _get(admin_client, PLACEMENTS, q=PASTED).context["cl"]
        assert changelist.result_count == 2


@pytest.mark.usefixtures("placements")
def test_sites_list(admin_client: Client) -> None:
    response = _get(admin_client, SITES, list="all", q=PASTED)
    assert _domains(response) == {"lensa.com", "veed.io"}
    assert "нет в списке (1): trueconf.com" in response.content.decode()


@pytest.mark.usefixtures("placements")
def test_catalog(admin_client: Client) -> None:
    response = _get(admin_client, CATALOG, q="lensa.com, amasty.com")
    assert _domains(response) == {"lensa.com", "amasty.com"}


@pytest.mark.usefixtures("placements")
def test_catalog_pasted_address_still_found(admin_client: Client) -> None:
    # Одно слово — как раньше: адрес целиком сводится к домену.
    assert "veed.io" in _domains(_get(admin_client, CATALOG, q="https://www.veed.io/blog/post"))


def test_other_lists_keep_one_line_field(admin_client: Client) -> None:
    page = _get(admin_client, KEYWORDS).content.decode()
    assert '<input type="text" size="40" name="q"' in page
    assert "data-domain-search" not in page
