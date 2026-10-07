"""Админка блока 2: размещение со ссылками, запрет удаления (E1-02)."""

import datetime as dt
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.keywords.models import Keyword
from apps.placements.models import Placement, PlacementLink
from apps.sites.models import Product, Seller, Site, SiteMetric, SitePrice

pytestmark = pytest.mark.django_db

ADD_URL = reverse("admin:placements_placement_add")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


def _keyword(product: Product, text: str) -> Keyword:
    return Keyword.objects.create(
        product=product, keyword=text, target_url=f"https://convertio.co/{text.replace(' ', '-')}/"
    )


def _form(
    site: Site, product: Product, links: list[dict[str, str]], initial: int = 0
) -> dict[str, str]:
    """Данные формы размещения, как их отправляет браузер."""
    data = {
        "site": str(site.pk),
        "product": str(product.pk),
        "status": "ordered",
        "currency": "EUR",
        "links-TOTAL_FORMS": str(len(links)),
        "links-INITIAL_FORMS": str(initial),
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
    for index, link in enumerate(links):
        data.update({f"links-{index}-{name}": value for name, value in link.items()})
    return data


def _link(keyword: Keyword | None, url: str = "") -> dict[str, str]:
    """Ссылка формы (E3-05): анкор из списка и куда ведёт; пусто — адрес анкора."""
    return {"keyword": str(keyword.pk) if keyword else "", "target_url": url}


class TestCreate:
    def test_new_placement_has_two_link_slots(self, admin_client: Client) -> None:
        response = admin_client.get(ADD_URL)
        assert response.context["inline_admin_formsets"][0].formset.total_form_count() == 2

    def test_two_links_bound_to_keywords(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        mp3 = _keyword(convertio, "mp4 to mp3")
        video = _keyword(convertio, "online video converter")
        links = [_link(mp3), _link(video, "https://convertio.co/video-converter/")]
        response = admin_client.post(ADD_URL, _form(site, convertio, links))
        assert response.status_code == 302
        placement = Placement.objects.get()
        assert (placement.site, placement.product, placement.status) == (site, convertio, "ordered")
        bound = placement.links.order_by("link_index").values_list(
            "link_index", "anchor", "keyword", "target_url", "anchor_type"
        )
        assert list(bound) == [
            # Адрес не вписали — подставился адрес анкора; номер — по порядку.
            (1, "mp4 to mp3", mp3.pk, "https://convertio.co/mp4-to-mp3/", "exact"),
            (
                2,
                "online video converter",
                video.pk,
                "https://convertio.co/video-converter/",
                "exact",
            ),
        ]
        # Свой адрес остался в ссылке, у анкора — прежний.
        video.refresh_from_db()
        assert video.target_url == "https://convertio.co/online-video-converter/"

    def test_url_without_anchor_is_error(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        links = [_link(None, "https://convertio.co/")]
        response = admin_client.post(ADD_URL, _form(site, convertio, links))
        assert response.status_code == 200
        errors = response.context["inline_admin_formsets"][0].formset.errors
        assert errors[0]["keyword"] == ["Выберите анкор."]

    def test_empty_slot_is_skipped(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        links = [_link(_keyword(convertio, "convert")), _link(None)]
        assert admin_client.post(ADD_URL, _form(site, convertio, links)).status_code == 302
        assert PlacementLink.objects.count() == 1

    def test_form_without_old_fields(self, admin_client: Client, convertio: Product) -> None:
        _keyword(convertio, "convert")
        page = admin_client.get(ADD_URL).content.decode()
        assert "data-anchor-select" in page and "data-anchor-url" in page
        assert 'name="links-0-anchor"' not in page
        assert 'name="links-0-link_index"' not in page
        assert 'name="links-0-extraction_test_passed"' not in page
        # Штатного «+» с окном Django у анкора больше нет.
        assert "add_id_links-0-keyword" not in page
        assert 'data-product="' in page and 'data-url="https://convertio.co/convert/"' in page

    def test_keyword_of_other_product_rejected(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        links = [_link(_keyword(clideo, "video editor"))]
        response = admin_client.post(ADD_URL, _form(site, convertio, links))
        assert response.status_code == 200
        errors = response.context["inline_admin_formsets"][0].formset.errors
        assert "keyword" in errors[0]
        assert not Placement.objects.exists()


class TestExisting:
    @pytest.fixture
    def link(self, site: Site, convertio: Product) -> PlacementLink:
        placement = Placement.objects.create(site=site, product=convertio)
        return placement.links.create(anchor="convertio.co", target_url="https://convertio.co/")

    def _post(self, client: Client, link: PlacementLink, **fields: str) -> None:
        url = reverse("admin:placements_placement_change", args=[link.placement_id])
        form_link = {"id": str(link.pk), "placement": str(link.placement_id), **fields}
        data = _form(link.placement.site, link.placement.product, [form_link], initial=1)
        assert client.post(url, data).status_code == 302

    def test_page_fields_are_read_only(self, admin_client: Client, link: PlacementLink) -> None:
        link.mark_lost(dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        self._post(
            admin_client,
            link,
            keyword="",
            target_url="https://convertio.co/",
            lost_at_0="2026-11-01",
            lost_at_1="00:00:00",
            char_offset="5",
        )
        link.refresh_from_db()
        # Старая ссылка без анкора из списка остаётся как была.
        assert (link.anchor, link.keyword_id) == ("convertio.co", None)
        assert link.lost_at == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        assert link.char_offset is None

    def test_legacy_link_shown_and_replaced(
        self, admin_client: Client, link: PlacementLink, convertio: Product
    ) -> None:
        url = reverse("admin:placements_placement_change", args=[link.placement_id])
        page = admin_client.get(url).content.decode()
        assert "«convertio.co» — нет в анкорах" in page
        brand = Keyword.objects.create(
            product=convertio,
            keyword="Convertio",
            target_url="https://convertio.co/",
            anchor_type="branded",
        )
        self._post(admin_client, link, keyword=str(brand.pk), target_url="https://convertio.co/")
        link.refresh_from_db()
        assert (link.anchor, link.keyword_id) == ("Convertio", brand.pk)
        assert link.anchor_type == "branded"

    def test_links_cannot_be_deleted(self, admin_client: Client, link: PlacementLink) -> None:
        url = reverse("admin:placements_placement_change", args=[link.placement_id])
        response = admin_client.get(url)
        assert response.context["inline_admin_formsets"][0].formset.can_delete is False
        self._post(admin_client, link, keyword="", target_url="https://x", DELETE="on")
        assert PlacementLink.objects.filter(pk=link.pk).exists()


@pytest.mark.parametrize(
    ("app", "model"),
    [
        ("placements", "placement"),
        ("keywords", "keyword"),
        ("keywords", "keywordposition"),
        ("keywords", "keywordcoverage"),
    ],
)
def test_delete_allowed(admin_client: Client, app: str, model: str) -> None:
    # Удалить можно любую запись (ADR-060); «Анкоры» удаляют сами ключи.
    response = admin_client.get(reverse(f"admin:{app}_{model}_changelist"))
    assert response.status_code == 200
    assert response.context["cl"].model_admin.has_delete_permission(response.wsgi_request) is True


def test_delete_page_deletes(admin_client: Client, site: Site, convertio: Product) -> None:
    placement = Placement.objects.create(site=site, product=convertio)
    url = reverse("admin:placements_placement_delete", args=[placement.pk])
    assert admin_client.post(url, {"post": "yes"}).status_code == 302
    assert not Placement.objects.filter(pk=placement.pk).exists()
    assert Site.objects.filter(pk=site.pk).exists()


class TestWorkingPriceColumns:
    def test_columns_use_working_price_and_current_metrics(
        self, admin_client: Client, site: Site, convertio: Product, django_assert_num_queries: Any
    ) -> None:
        working = Seller.objects.create(name="Working seller")
        historical = Seller.objects.create(name="Placement seller")
        price = SitePrice.objects.create(
            site=site,
            seller=working,
            placement_cents=12300,
            announce_cents=0,
            writing_cents=4500,
            currency="USD",
        )
        site.price = price
        site.save(update_fields=["price"])
        SiteMetric.objects.create(
            site=site, dr=59, organic_traffic=22961, top_geo="us", top_geo_traffic=13921
        )
        Placement.objects.create(
            site=site, product=convertio, seller=historical, price_paid_cents=5000, currency="EUR"
        )
        response = admin_client.get(reverse("admin:placements_placement_changelist"))
        assert response.status_code == 200
        changelist = response.context["cl"]
        columns = changelist.list_display
        start = columns.index("site_link")
        assert columns[start : start + 6] == [
            "site_link",
            "working_seller_cell",
            "status_link",
            "dr_cell",
            "traffic_cell",
            "top_geo_cell",
        ]
        paid = columns.index("paid_cell")
        assert columns[paid - 3 : paid] == [
            "publication_price_cell",
            "announce_price_cell",
            "writing_price_cell",
        ]
        row = next(iter(changelist.result_list))
        admin = changelist.model_admin
        # Все семь колонок читаются из одной выборки, без запросов на каждую строку.
        with django_assert_num_queries(0):
            seller_html = admin.working_seller_cell(row)
            assert reverse("admin:sites_seller_change", args=[working.pk]) in seller_html
            assert "data-panel" in seller_html
            assert ">Working seller</a>" in seller_html
            assert admin.dr_cell(row) == 59
            assert admin.traffic_cell(row) == 22961
            assert "US 13921" in admin.top_geo_cell(row)
            assert admin.publication_price_cell(row) == "$123"
            assert admin.announce_price_cell(row) == "$0"
            assert admin.writing_price_cell(row) == "$45"
            assert admin.paid_cell(row) == "€50"

    def test_without_working_price_shows_placement_seller_with_note(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        seller = Seller.objects.create(name="Placement seller")
        Placement.objects.create(site=site, product=convertio, seller=seller)
        response = admin_client.get(reverse("admin:placements_placement_changelist"))
        row = next(iter(response.context["cl"].result_list))
        admin = response.context["cl"].model_admin
        seller_html = admin.working_seller_cell(row)
        assert ">Placement seller</a>" in seller_html
        assert "рабочая цена не выбрана" in seller_html
        assert reverse("admin:sites_seller_change", args=[seller.pk]) in seller_html
        assert admin.publication_price_cell(row) == ""
        assert admin.announce_price_cell(row) == ""
        assert admin.writing_price_cell(row) == ""
        assert admin.dr_cell(row) == ""
        assert admin.traffic_cell(row) == ""
        assert admin.top_geo_cell(row) == ""
