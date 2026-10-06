"""Экраны цен и заметок: «Площадки», карточка площадки, продавцы (E1-07, ADR-043)."""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.sites.models import (
    ExchangeRate,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
)
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db

OfferFactory = Callable[..., SitePrice]
URL = reverse("admin:sites_productsitelatest_changelist")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def linkhub() -> Seller:
    return Seller.objects.create(name="LinkHub Media", currency="USD")


@pytest.fixture
def starlinks() -> Seller:
    return Seller.objects.create(name="Starlinks", currency="EUR")


@pytest.fixture
def site(convertio: Product) -> Site:
    site = Site.objects.create(domain="example.com", language="en")
    SiteMetric.objects.create(site=site, dr=60)
    return site


@pytest.fixture
def two_sellers(
    site: Site, linkhub: Seller, starlinks: Seller, offer: OfferFactory
) -> tuple[SitePrice, SitePrice, SitePrice]:
    """Collaborator €200 (рабочая), LinkHub $170, Starlinks €180; курс 1.1355."""
    ExchangeRate.objects.create(
        currency="USD", rate_date=dt.date(2026, 9, 30), rate=Decimal("1.1355")
    )
    working = offer(site, 20000, writing_cents=1000)
    dollars = offer(site, 17000, seller=linkhub, currency="USD", working=False)
    euros = offer(site, 18000, seller=starlinks, working=False)
    return working, dollars, euros


def _page(client: Client, **params: str) -> str:
    response = client.get(URL, {"list": "all", **params})
    assert response.status_code == 200
    return response.content.decode()


def _card(client: Client, site: Site, partial: bool = True) -> str:
    url = reverse("admin:sites_site_card", args=[site.pk])
    headers = {PARTIAL_HEADER: "1"} if partial else {}
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    return response.content.decode()


class TestSitesList:
    @pytest.mark.usefixtures("two_sellers")
    def test_working_price_seller_and_cheaper(self, admin_client: Client) -> None:
        """Критерий приёмки: рабочая цена с продавцом и услугой, «дешевле» с разницей,
        доллары пересчитаны по курсу, исходная цена видна."""
        content = _page(admin_client)
        assert "<b>€200</b>" in content
        assert "публикация · Collaborator" in content
        # $170 ≈ €149.71: дешевле рабочей на ≈ €50.
        assert "▼ ≈€50 · LinkHub Media" in content
        assert "📝 €10" in content
        # Все цены — в подсказке, исходная цена с пересчётом.
        assert "LinkHub Media · публикация $170 ≈ €150" in content
        assert "Starlinks · публикация €180" in content

    def test_dates_in_project_time(
        self, admin_client: Client, site: Site, starlinks: Seller, offer: OfferFactory
    ) -> None:
        # 27.09 00:00 по Москве — в базе 26.09 21:00 UTC; показываем 27.09.
        moscow = dt.timezone(dt.timedelta(hours=3))
        offer(site, 20000, checked_at=dt.datetime(2026, 9, 27, tzinfo=moscow))
        offer(site, 25000, seller=starlinks, working=False)
        content = _page(admin_client)
        assert "27.09.2026" in content
        assert "26.09.2026" not in content

    @pytest.mark.usefixtures("two_sellers")
    def test_offers_filters(
        self, admin_client: Client, linkhub: Seller, offer: OfferFactory
    ) -> None:
        plain = Site.objects.create(domain="plain.com", language="en")
        offer(plain, 10000)
        Site.objects.create(domain="noprice.com", language="en")
        url_domains = {
            key: _page(admin_client, offers=key) for key in ("pending", "cheaper", "noprice")
        }
        assert "example.com" in url_domains["cheaper"]
        assert "plain.com" not in url_domains["cheaper"]
        assert "example.com" in url_domains["pending"]
        assert "noprice.com" in url_domains["noprice"]
        assert "example.com" not in url_domains["noprice"]
        by_seller = _page(admin_client, seller=str(linkhub.pk))
        assert "example.com" in by_seller and "plain.com" not in by_seller

    def test_seller_metrics_marked(
        self, admin_client: Client, convertio: Product, linkhub: Seller
    ) -> None:
        site = Site.objects.create(domain="told.com", language="en")
        SiteMetric.objects.create(site=site, dr=62, seller=linkhub)
        content = _page(admin_client)
        assert "по словам продавца LinkHub Media" in content

    def test_notes_button(self, admin_client: Client, site: Site) -> None:
        SiteNote.objects.create(site=site, body="Отвечают быстро")
        content = _page(admin_client)
        assert "💬 1" in content
        assert "Отвечают быстро" in content


def _action(client: Client, action: str, site: Site, **extra: str) -> str:
    row = ProductSite.objects.get(site=site, product__domain="convertio.co")
    response = client.post(
        URL + "?list=all",
        {"action": action, "_selected_action": [str(row.pk)], "index": "0", **extra},
        follow=True,
    )
    assert response.status_code == 200
    return response.content.decode()


class TestActions:
    def test_fix_seller(
        self,
        admin_client: Client,
        site: Site,
        linkhub: Seller,
        two_sellers: tuple[SitePrice, ...],
    ) -> None:
        _, dollars, _ = two_sellers
        content = _action(admin_client, "fix_seller_action", site, seller=str(linkhub.pk))
        assert "Рабочая цена изменена: 1." in content
        site.refresh_from_db()
        assert site.price_id == dollars.pk
        assert SiteNote.objects.get(site=site).author is not None

    def test_fix_seller_needs_seller(
        self, admin_client: Client, site: Site, two_sellers: tuple[SitePrice, ...]
    ) -> None:
        content = _action(admin_client, "fix_seller_action", site)
        assert "Выберите продавца" in content

    def test_accept_new_prices(self, admin_client: Client, site: Site, offer: OfferFactory) -> None:
        offer(site, 20000, checked_at=dt.datetime(2026, 9, 27, tzinfo=dt.UTC))
        new = offer(site, 23000, working=False, checked_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        assert "▲ новая €230" in _page(admin_client)
        _action(admin_client, "accept_new_prices_action", site)
        site.refresh_from_db()
        assert site.price_id == new.pk

    def test_keep_current(
        self, admin_client: Client, site: Site, two_sellers: tuple[SitePrice, ...]
    ) -> None:
        working, _, _ = two_sellers
        content = _action(admin_client, "keep_current_action", site)
        assert "Разобрано: 1." in content
        site.refresh_from_db()
        assert site.price_id == working.pk
        assert not SitePrice.objects.filter(reviewed_at__isnull=True).exists()


class TestCard:
    def test_card_parts(
        self, admin_client: Client, site: Site, two_sellers: tuple[SitePrice, ...]
    ) -> None:
        offers_ = _card(admin_client, site)
        assert "Цена и предложения" in offers_
        assert "Сделать рабочей" in offers_
        assert "рабочая" in offers_
        assert "$170" in offers_ and "≈&nbsp;€150" in offers_
        assert "<html" not in offers_
        assert "<html" in _card(admin_client, site, partial=False)

    def test_fix_from_card(
        self, admin_client: Client, site: Site, two_sellers: tuple[SitePrice, ...]
    ) -> None:
        _, _, euros = two_sellers
        url = reverse("admin:sites_site_card_fix", args=[site.pk])
        response = admin_client.post(url, {"offer": euros.pk}, headers={PARTIAL_HEADER: "1"})
        assert response.status_code == 200
        assert "Цена: Collaborator · публикация €200 → Starlinks · публикация €180" in (
            response.content.decode()
        )
        site.refresh_from_db()
        assert site.price_id == euros.pk

    def test_offer_of_other_site_is_refused(
        self, admin_client: Client, site: Site, offer: OfferFactory
    ) -> None:
        other = offer(Site.objects.create(domain="other.com"), 5000)
        url = reverse("admin:sites_site_card_fix", args=[site.pk])
        assert admin_client.post(url, {"offer": other.pk}).status_code == 404

    def test_add_note_with_author(
        self, admin_client: Client, admin_user: User, site: Site, convertio: Product
    ) -> None:
        url = reverse("admin:sites_site_card_note", args=[site.pk])
        response = admin_client.post(url, {"body": "Обещали скидку", "product": convertio.pk})
        assert response.status_code == 302
        note = SiteNote.objects.get(site=site)
        assert (note.body, note.author, note.product) == ("Обещали скидку", admin_user, convertio)
        assert "Обещали скидку" in _card(admin_client, site)

    def test_notes_are_not_deleted_or_edited(self) -> None:
        # Отдельного экрана заметок нет: только карточка, где можно лишь добавить.
        assert SiteNote not in admin.site._registry

    def test_rejection_reason_goes_to_history(
        self, admin_client: Client, admin_user: User, site: Site, convertio: Product
    ) -> None:
        row = ProductSite.objects.get(site=site, product=convertio)
        url = reverse("admin:sites_productsite_change", args=[row.pk])
        response = admin_client.post(
            url, {"status": SiteStatus.DISCARDED, "comment": "Не тематика"}
        )
        assert response.status_code == 302
        note = SiteNote.objects.get(site=site)
        assert (note.body, note.product, note.author) == ("Не тематика", convertio, admin_user)


class TestSellersAndPrices:
    def test_first_manual_price_becomes_working(
        self, admin_client: Client, site: Site, linkhub: Seller
    ) -> None:
        url = reverse("admin:sites_siteprice_add")
        response = admin_client.post(
            url,
            {
                "site": site.pk,
                "seller": linkhub.pk,
                "placement_type": "guest_post",
                "placement_cents": 15000,
                "currency": "USD",
                "source": "manual",
            },
        )
        assert response.status_code == 302
        site.refresh_from_db()
        assert site.price is not None and site.price.seller == linkhub

    def test_sellers_list(self, admin_client: Client, linkhub: Seller, offer: OfferFactory) -> None:
        offer(Site.objects.create(domain="a.com"), 10000, seller=linkhub, currency="USD")
        response = admin_client.get(reverse("admin:sites_seller_changelist"))
        content = response.content.decode()
        assert "LinkHub Media" in content and "Collaborator" in content

    def test_new_site_form_opens(self, admin_client: Client) -> None:
        response = admin_client.get(reverse("admin:sites_site_add"))
        assert response.status_code == 200
