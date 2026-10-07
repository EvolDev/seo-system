"""Карточка продавца и фильтр продавцов (E1-24).

Валюта прайсов выбирается списком, а не вводится руками: код из трёх букв
приходилось угадывать. Длинный фильтр продавцов получил поиск: их полсотни.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.sites.admin import SellerFilter, SellerForm
from apps.sites.models import Product, Seller, SellerRating
from apps.sites.offers import CURRENCIES

pytestmark = pytest.mark.django_db

ADD = reverse("admin:sites_seller_add")


def _codes(form: SellerForm) -> list[str]:
    """Коды валют из выбора формы."""
    field: Any = form.fields["currency"]
    return [str(code) for code, _ in field.choices]


SITES = reverse("admin:sites_productsitelatest_changelist")


class TestCurrencyChoice:
    def test_currency_is_a_select_with_search(self, admin_client: Client) -> None:
        page = admin_client.get(ADD).content.decode()
        block = page[page.index('name="currency"') - 200 : page.index('name="currency"') + 400]
        assert "<select" in block
        assert "data-search" in block

    def test_known_currencies_are_offered(self) -> None:
        form = SellerForm()
        offered = _codes(form)
        assert set(CURRENCIES) <= set(offered)

    def test_own_currency_is_kept_even_if_unknown(self) -> None:
        """Старое значение из импорта не должно пропасть из списка."""
        seller = Seller.objects.create(name="Odd", currency="SEK")
        form = SellerForm(instance=seller)
        offered = _codes(form)
        assert "SEK" in offered

    def test_new_seller_gets_euro(self, admin_client: Client) -> None:
        """Евро у новой записи — значение по умолчанию поля модели."""
        page = admin_client.get(ADD).content.decode()
        assert '<option value="EUR" selected>EUR</option>' in page

    def test_chosen_currency_is_saved(self, admin_client: Client) -> None:
        admin_client.post(
            ADD, {"name": "Nick Hemenway", "currency": "USD", "contacts": "", "notes": ""}
        )
        assert Seller.objects.get(name="Nick Hemenway").currency == "USD"


class TestFilterSearch:
    def test_short_list_has_no_search(self, admin_client: Client) -> None:
        """На коротком списке поле поиска только мешает."""
        Product.objects.create(name="Convertio", domain="convertio.co")
        for number in range(3):
            Seller.objects.create(name=f"Seller {number}")
        page = admin_client.get(SITES).content.decode()
        assert "seo-multi-search" not in page

    def test_long_list_gets_search(self, admin_client: Client) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        for number in range(SellerFilter.search_from):
            Seller.objects.create(name=f"Seller {number:02}")
        page = admin_client.get(SITES).content.decode()
        assert "seo-multi-search" in page


class TestRating:
    """Оценка продавца звёздочкой — по образцу площадок (E1-24, ADR-064)."""

    @pytest.fixture
    def seller(self) -> Seller:
        return Seller.objects.create(name="Zain MediaX")

    def _url(self, seller: Seller) -> str:
        return reverse("admin:sites_seller_rate", args=[seller.pk])

    def test_rating_is_saved_and_returned(self, admin_client: Client, seller: Seller) -> None:
        response = admin_client.post(self._url(seller), {"value": "4"})
        assert response.json() == {
            "average": "4.0",
            "count": 1,
            "mine": 4,
            "title": "4.0 · оценок: 1 · ваша 4",
        }

    def test_second_rating_replaces_the_first(self, admin_client: Client, seller: Seller) -> None:
        admin_client.post(self._url(seller), {"value": "2"})
        admin_client.post(self._url(seller), {"value": "5"})
        assert SellerRating.objects.filter(seller=seller).count() == 1
        assert SellerRating.objects.get(seller=seller).value == 5

    def test_average_counts_every_person(self, admin_client: Client, seller: Seller) -> None:
        admin_client.post(self._url(seller), {"value": "5"})
        other = User.objects.create_user("colleague", password="x", is_staff=True)
        other.user_permissions.add(Permission.objects.get(codename="change_seller"))
        client = Client()
        client.force_login(other)
        response = client.post(self._url(seller), {"value": "2"})
        assert response.json()["average"] == "3.5"

    def test_value_out_of_range_is_refused(self, admin_client: Client, seller: Seller) -> None:
        admin_client.post(self._url(seller), {"value": "4"})
        assert admin_client.post(self._url(seller), {"value": "9"}).status_code == 400
        assert SellerRating.objects.get(seller=seller).value == 4

    def test_empty_value_removes_the_rating(self, admin_client: Client, seller: Seller) -> None:
        admin_client.post(self._url(seller), {"value": "3"})
        admin_client.post(self._url(seller), {"value": ""})
        assert not SellerRating.objects.exists()

    def test_without_permission_nothing_is_written(self, seller: Seller) -> None:
        looker = User.objects.create_user("looker", password="x", is_staff=True)
        looker.user_permissions.add(Permission.objects.get(codename="view_seller"))
        client = Client()
        client.force_login(looker)
        assert client.post(self._url(seller), {"value": "5"}).status_code == 403
        assert not SellerRating.objects.exists()

    def test_star_stands_before_the_name(self, admin_client: Client, seller: Seller) -> None:
        admin_client.post(self._url(seller), {"value": "4"})
        page = admin_client.get(reverse("admin:sites_seller_changelist")).content.decode()
        star = page.index('class="seo-rating"')
        assert star < page.index("Zain MediaX", star)
        assert '<span class="seo-rating-value">4.0</span>' in page

    def test_seller_without_ratings_shows_no_number(
        self, admin_client: Client, seller: Seller
    ) -> None:
        page = admin_client.get(reverse("admin:sites_seller_changelist")).content.decode()
        assert "seo-rating-empty" in page
        assert 'class="seo-rating-value"' not in page

    def test_hint_says_seller_not_site(self, admin_client: Client, seller: Seller) -> None:
        """«Оценить продавца», а не «площадку»: звезда та же, предмет разный."""
        page = admin_client.get(reverse("admin:sites_seller_changelist")).content.decode()
        assert "Оценить продавца" in page
        assert "Оценить площадку" not in page

    def test_star_in_the_seller_card(self, admin_client: Client, seller: Seller) -> None:
        """Карточка продавца: звезда у имени (просьба пользователя 07.10.2026)."""
        admin_client.post(self._url(seller), {"value": "5"})
        url = reverse("admin:sites_seller_change", args=[seller.pk])
        page = admin_client.get(url).content.decode()
        head = page[page.index("<h2>") : page.index("</h2>")]
        assert "seo-rating" in head
        assert "5.0" in head


class TestInSiteCard:
    """Продавец в таблице предложений карточки площадки (E1-24)."""

    @pytest.fixture
    def offer(self, db: None) -> Any:
        import datetime as dt

        from django.utils import timezone

        from apps.sites.models import MetricSource, Site, SitePrice

        Product.objects.create(name="Convertio", domain="convertio.co")
        seller = Seller.objects.create(name="Athena Smith")
        site = Site.objects.create(domain="donor.com")
        price = SitePrice.objects.create(
            site=site,
            seller=seller,
            placement_cents=20000,
            currency="EUR",
            source=MetricSource.MANUAL,
            checked_at=timezone.make_aware(dt.datetime(2026, 10, 1, 12, 0)),
            reviewed_at=timezone.now(),
        )
        site.price = price
        site.save(update_fields=["price"])
        return site, seller

    def test_name_opens_the_seller_card_in_a_new_tab(
        self, admin_client: Client, offer: Any
    ) -> None:
        """Карточка площадки живёт в панели — уводить её на продавца нельзя."""
        site, seller = offer
        page = admin_client.get(reverse("admin:sites_site_card", args=[site.pk])).content.decode()
        table = page[page.index("seo-card-table") :]
        link = reverse("admin:sites_seller_change", args=[seller.pk])
        assert f'href="{link}" target="_blank"' in table

    def test_star_stands_before_the_seller(self, admin_client: Client, offer: Any) -> None:
        site, seller = offer
        admin_client.post(reverse("admin:sites_seller_rate", args=[seller.pk]), {"value": "4"})
        page = admin_client.get(reverse("admin:sites_site_card", args=[site.pk])).content.decode()
        table = page[page.index("seo-card-table") :]
        star = table.index('class="seo-rating"')
        assert star < table.index("Athena Smith", star)
        assert '<span class="seo-rating-value">4.0</span>' in table
