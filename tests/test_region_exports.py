"""Регион размещения в «Размещениях» и выгрузке (ADR-076) и топ-регион площадки в
угадывании языка — у списка анкоров и у набора условий серости."""

import json
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.keywords import regions
from apps.keywords.models import AnchorList
from apps.placements import export as placement_export
from apps.placements import export_designer as design
from apps.placements.models import Placement, PlacementStatus
from apps.sites import latest
from apps.sites.languages import site_language
from apps.sites.models import GrayTermSet, Product, Site, SiteCountryMetric, SiteMetric
from apps.workspace.products import choose_product
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db

LIST_URL = reverse("admin:placements_placement_changelist")


@pytest.fixture
def convertio(admin_user: User) -> Product:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    choose_product(admin_user, product)
    return product


@pytest.fixture
def lists(convertio: Product) -> tuple[AnchorList, AnchorList]:
    us = AnchorList.objects.create(product=convertio, country="us")
    return us, AnchorList.objects.create(product=convertio, country="br")


def _site(domain: str, *, language: str | None = None, top_geo: str | None = None) -> Site:
    site = Site.objects.create(domain=domain, language=language)
    if top_geo:
        SiteMetric.objects.create(
            site=site, dr=40, organic_traffic=1000, top_geo=top_geo, top_geo_traffic=800
        )
        latest.refresh([site.pk])
    return site


def _bootstrap(html: str) -> dict[str, Any]:
    """Настройки конструктора со страницы — `json_script` «export-bootstrap»."""
    start = html.index('id="export-bootstrap"')
    start = html.index(">", start) + 1
    loaded: dict[str, Any] = json.loads(html[start : html.index("</script>", start)])
    return loaded


def _placement(product: Product, site: Site, **fields: Any) -> Placement:
    fields.setdefault("status", PlacementStatus.ORDERED)
    return Placement.objects.create(site=site, product=product, **fields)


class TestTopGeo:
    @pytest.mark.parametrize(
        ("domain", "language", "top_geo", "expected"),
        [
            ("acritica.net", None, "br", ("pt", "по топ-региону площадки (Бразилия)")),
            # Язык из карточки и зона домена — главнее топ-региона.
            ("example.com", "en", "br", ("en", "по языку площадки")),
            ("segs.com.br", None, "us", ("pt", "по домену .br")),
            ("heygen.com", None, "in", ("en", "по топ-региону площадки (Индия)")),
            ("rezerv.co", None, None, (None, "")),
        ],
    )
    def test_language_order(
        self, domain: str, language: str | None, top_geo: str | None, expected: tuple[Any, str]
    ) -> None:
        assert site_language(Site(domain=domain, language=language), top_geo) == expected

    def test_anchor_region_by_top_geo(
        self, convertio: Product, lists: tuple[AnchorList, AnchorList]
    ) -> None:
        _, br = lists
        placement = _placement(convertio, _site("acritica.net", top_geo="br"))
        choice = regions.for_placement(placement)
        assert (choice.anchor_list, choice.reason) == (br, "по топ-региону площадки (Бразилия)")

    def test_gray_set_by_top_geo(self, admin_client: Client, convertio: Product) -> None:
        site = _site("acritica.net", top_geo="br")
        url = reverse("admin:sites_site_card", args=[site.pk])
        page = admin_client.get(url, headers={PARTIAL_HEADER: "1"}).content.decode()
        assert GrayTermSet.objects.filter(language="pt").exists()
        assert "по топ-региону площадки (Бразилия)" in page
        assert "&quot;cassino&quot;" in page


class TestRegionFilter:
    @pytest.fixture
    def placements(
        self, convertio: Product, lists: tuple[AnchorList, AnchorList]
    ) -> dict[str, Placement]:
        _, br = lists
        return {
            "dci": _placement(convertio, _site("dci.com.br")),
            "acritica": _placement(convertio, _site("acritica.net", top_geo="br")),
            "srzd": _placement(convertio, _site("srzd.com", language="en"), anchor_list=br),
            "example": _placement(convertio, _site("example.com", language="en")),
            # Ни языка, ни домена, ни топ-региона — основной список по умолчанию.
            "unknown": _placement(convertio, _site("rezerv.co")),
        }

    def _domains(self, client: Client, **params: str) -> set[str]:
        response = client.get(LIST_URL, params)
        return {row.site.domain for row in response.context["cl"].result_list}

    def test_brazil_usa_and_unknown(
        self, admin_client: Client, placements: dict[str, Placement]
    ) -> None:
        assert self._domains(admin_client, region="br") == {
            "dci.com.br",
            "acritica.net",
            "srzd.com",
        }
        assert self._domains(admin_client, region="us") == {"example.com"}
        assert self._domains(admin_client, region="unknown") == {"rezerv.co"}
        assert len(self._domains(admin_client)) == 5

    def test_choices_with_flags(
        self, admin_client: Client, placements: dict[str, Placement]
    ) -> None:
        html = admin_client.get(LIST_URL).content.decode()
        filters = html[html.index('class="list-filter-dropdown seo-region-filter"') :]
        filters = filters[: filters.index("</select>")]
        assert "Все регионы" in filters and "Не определён" in filters
        assert 'data-flag="br"' in filters and 'data-flag="us"' in filters


class TestExport:
    @pytest.fixture
    def traffic(self, convertio: Product, lists: tuple[AnchorList, AnchorList]) -> None:
        brazil = _site("dci.com.br")
        usa = _site("example.com", language="en")
        SiteCountryMetric.objects.create(site=brazil, country="br", organic_traffic=5000)
        SiteCountryMetric.objects.create(site=brazil, country="us", organic_traffic=30)
        SiteCountryMetric.objects.create(site=usa, country="us", organic_traffic=7000)
        _placement(convertio, brazil)
        _placement(convertio, usa)
        _placement(convertio, _site("nodata.com.br"))
        unknown = _site("rezerv.co")
        SiteCountryMetric.objects.create(site=unknown, country="us", organic_traffic=99)
        _placement(convertio, unknown)

    @pytest.mark.usefixtures("traffic")
    def test_sheet_columns(self) -> None:
        wanted = {"target", "us_traffic", "region", "region_traffic"}
        sheet = placement_export.sheets(Placement.objects.order_by("pk"), wanted)[0]
        assert [c.title for c in sheet.columns] == ["Target", "US Traff", "Region", "Region Traff"]
        assert sheet.rows == [
            ["dci.com.br", 30, "br", 5000],
            ["example.com", 7000, "us", 7000],
            # Замера Бразилии нет — пусто, не ноль.
            ["nodata.com.br", None, "br", None],
            # Регион не известен — обе колонки пусты; «US Traff» — США, как всегда.
            ["rezerv.co", 99, None, None],
        ]

    @pytest.mark.usefixtures("traffic")
    def test_designer_fields_and_preview(self) -> None:
        registry = design.fields()
        assert (registry["region"].header, registry["region_traffic"].header) == (
            "Region",
            "Region Traff",
        )
        value = design.initial(registry)
        value["columns"] = [
            {"id": str(n), "index": None, "header": registry[key].header, "source": key,
             "enabled": True, "constant": ""}
            for n, key in enumerate(("target", "region_traffic", "region"))
        ]  # fmt: skip
        records = design.dataset(Placement.objects.order_by("pk"), registry)
        plan = design.build_plan(records, design.validate(value, registry), registry)
        # Заголовок постоянный: любой регион ложится в ту же колонку.
        assert [c.title for c in plan.sheet.columns] == ["Target", "Region Traff", "Region"]
        assert [list(row) for row in plan.sheet.rows] == [
            ["dci.com.br", 5000, "br"],
            ["example.com", 7000, "us"],
            ["nodata.com.br", None, "br"],
            ["rezerv.co", None, None],
        ]

    def test_my_table_preset_has_region_columns(self) -> None:
        keys = list(design.TEMPLATE_KEYS)
        assert "us_traffic" not in keys
        at = keys.index("region_traffic")
        assert keys[at - 1 : at + 2] == ["top_geo_traffic", "region_traffic", "region"]
        headers = [c["header"] for c in design.initial(design.fields())["columns"]]
        assert headers[at : at + 2] == ["Region Traff", "Region"]

    def test_old_region_setting_is_dropped(self) -> None:
        registry = design.fields()
        value = {**design.initial(registry), "region": "br"}
        assert "region" not in design.validate(value, registry)
