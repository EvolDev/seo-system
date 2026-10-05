"""Рабочий продукт пользователя (E9-12, ADR-057): выбор в шапке, фильтры, новые записи."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.keywords.models import Keyword
from apps.placements import home
from apps.placements.models import Invoice, InvoiceItem, Placement, PlacementStatus
from apps.sites.models import Product, Seller, Site, SiteList, SiteListItem
from apps.workspace.models import UserSettings
from apps.workspace.products import (
    choose_product,
    chosen_product_id,
    without_product,
    working_product_id,
)

pytestmark = pytest.mark.django_db

PLACEMENTS = reverse("admin:placements_placement_changelist")
SITES = reverse("admin:sites_productsitelatest_changelist")
INVOICES = reverse("admin:placements_invoice_changelist")
KEYWORDS = reverse("admin:keywords_keyword_changelist")
SWITCH = reverse("admin:working_product")


@pytest.fixture
def products() -> tuple[Product, Product]:
    return (
        Product.objects.create(name="Convertio", domain="convertio.co"),
        Product.objects.create(name="Clideo", domain="clideo.com"),
    )


@pytest.fixture
def placements(products: tuple[Product, Product]) -> dict[str, Placement]:
    convertio, clideo = products
    return {
        name: Placement.objects.create(
            site=Site.objects.create(domain=f"{name}.com"),
            product=product,
            status=PlacementStatus.PUBLISHED,
        )
        for name, product in (("conv", convertio), ("clid", clideo))
    }


def _rows(response: Any) -> set[str]:
    return {row.site.domain for row in response.context["cl"].result_list}


def _request(user: User) -> Any:
    request = RequestFactory().get("/")
    request.user = user
    return request


class TestWorkingProduct:
    def test_first_active_until_chosen(
        self, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        convertio, clideo = products
        convertio.is_active = False
        convertio.save()
        assert working_product_id(_request(admin_user)) == clideo.pk
        assert chosen_product_id(admin_user) is None

    def test_choice_is_personal(
        self, admin_user: User, django_user_model: Any, products: tuple[Product, Product]
    ) -> None:
        convertio, clideo = products
        other = django_user_model.objects.create_user("kate", password="x", is_staff=True)
        choose_product(admin_user, clideo)
        assert working_product_id(_request(admin_user)) == clideo.pk
        assert working_product_id(_request(other)) == convertio.pk
        choose_product(admin_user, convertio)
        assert UserSettings.objects.get(user=admin_user).product == convertio

    def test_deleted_product_falls_back(
        self, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        convertio, clideo = products
        choose_product(admin_user, clideo)
        clideo.delete_unused()
        assert UserSettings.objects.get(user=admin_user).product is None
        assert working_product_id(_request(admin_user)) == convertio.pk

    def test_without_product_drops_choice_and_page(self) -> None:
        query = QueryDict(
            "product=2&product__id__exact=1&keyword__product__id__exact=1&p=3&q=a&o=1"
        )
        assert without_product(query) == "q=a&o=1"


class TestSwitch:
    def test_saves_and_returns_without_product(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        _, clideo = products
        response = admin_client.post(
            SWITCH,
            {
                "working_product": clideo.pk,
                "next": f"{PLACEMENTS}?product__id__exact=1&status=x&p=2",
            },
        )
        assert response.status_code == 302
        assert response["Location"] == f"{PLACEMENTS}?status=x"
        assert chosen_product_id(admin_user) == clideo.pk

    def test_foreign_next_goes_home(
        self, admin_client: Client, products: tuple[Product, Product]
    ) -> None:
        response = admin_client.post(
            SWITCH, {"working_product": products[0].pk, "next": "//evil.com/x"}
        )
        assert response["Location"] == reverse("admin:index")

    def test_unknown_product(self, admin_client: Client) -> None:
        assert admin_client.post(SWITCH, {"working_product": "999"}).status_code == 404

    def test_header_shows_current(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        convertio, clideo = products
        choose_product(admin_user, clideo)
        page = admin_client.get(reverse("admin:index")).content.decode()
        assert 'class="seo-product-switch"' in page
        assert f'<option value="{clideo.pk}" selected>Clideo</option>' in page
        assert f'<option value="{convertio.pk}">Convertio</option>' in page

    def test_no_switch_on_login(self, client: Client, products: tuple[Product, Product]) -> None:
        page = client.get(reverse("admin:login")).content.decode()
        assert "seo-product-switch" not in page


class TestFilters:
    def test_placements_default_and_all(
        self,
        admin_client: Client,
        admin_user: User,
        products: tuple[Product, Product],
        placements: dict[str, Placement],
    ) -> None:
        convertio, clideo = products
        assert _rows(admin_client.get(PLACEMENTS)) == {"conv.com"}
        choose_product(admin_user, clideo)
        assert _rows(admin_client.get(PLACEMENTS)) == {"clid.com"}
        # Явный выбор в колонке главнее рабочего; «Все» — своим значением.
        assert _rows(admin_client.get(PLACEMENTS, {"product__id__exact": convertio.pk})) == {
            "conv.com"
        }
        assert _rows(admin_client.get(PLACEMENTS, {"product__id__exact": "all"})) == {
            "conv.com",
            "clid.com",
        }
        page = admin_client.get(PLACEMENTS).content.decode()
        assert "?product__id__exact=all" in page

    def test_sites_default(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        _, clideo = products
        site_list = SiteList.objects.create(name="Октябрь")
        SiteListItem.objects.create(site_list=site_list, site=Site.objects.create(domain="a.com"))
        choose_product(admin_user, clideo)
        rows = admin_client.get(SITES).context["cl"].result_list
        assert {row.product_id for row in rows} == {clideo.pk}

    def test_invoices_default(
        self,
        admin_client: Client,
        admin_user: User,
        products: tuple[Product, Product],
        placements: dict[str, Placement],
    ) -> None:
        seller = Seller.objects.order_by("pk").first()
        assert seller is not None
        for placement in placements.values():
            invoice = Invoice.objects.create(seller=seller, amount_cents=100, currency="EUR")
            InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=100)
        choose_product(admin_user, products[1])
        listed = admin_client.get(INVOICES).context["cl"].result_list
        assert [item.placement.product_id for i in listed for item in i.items.all()] == [
            products[1].pk
        ]
        assert len(admin_client.get(INVOICES, {"product": "all"}).context["cl"].result_list) == 2

    def test_keywords_default(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        convertio, clideo = products
        Keyword.objects.create(product=convertio, keyword="pdf to word", target_url="https://c.co/")
        Keyword.objects.create(product=clideo, keyword="cut video", target_url="https://cl.com/")
        choose_product(admin_user, clideo)
        listed = admin_client.get(KEYWORDS).context["cl"].result_list
        assert [k.keyword for k in listed] == ["cut video"]


class TestNewRecords:
    def test_placement_add_has_working_product(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        choose_product(admin_user, products[1])
        form = admin_client.get(reverse("admin:placements_placement_add")).context["adminform"]
        assert form.form.initial["product"] == products[1].pk

    def test_upload_form_has_working_product(
        self, admin_client: Client, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        choose_product(admin_user, products[1])
        response = admin_client.get(reverse("admin:sites_upload_add"))
        assert response.context["form"].initial["product"] == products[1].pk


class TestHome:
    def test_stats_follow_working_product(
        self, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        _, clideo = products
        choose_product(admin_user, clideo)
        stats = home.context(_request(admin_user))["home_stats"]
        chosen = [name for _, name, _, selected in stats["products"] if selected]
        assert chosen == ["Clideo"]

    def test_all_products_explicit(
        self, admin_user: User, products: tuple[Product, Product]
    ) -> None:
        request = RequestFactory().get("/", {"product": "all"})
        request.user = admin_user
        stats = home.context(request)["home_stats"]
        chosen = [name for _, name, _, selected in stats["products"] if selected]
        assert chosen == ["Все продукты"]
        assert "product=all" in stats["products"][0][2]
