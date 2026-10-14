"""Ручная правка цены сохраняет снимки, условия продавца и договорённые оплаты."""

from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement
from apps.sites import offers
from apps.sites.models import MetricSource, Product, ProductSiteLatest, Seller, Site, SitePrice

pytestmark = pytest.mark.django_db


@pytest.fixture
def price() -> SitePrice:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    site = Site.objects.create(domain="editor.example")
    price = SitePrice.objects.create(
        site=site,
        seller=Seller.objects.create(name="Katerina"),
        placement_cents=80000,
        currency="USD",
        writing_cents=12000,
        announce_cents=0,
        gray_cents=150000,
        link_type="dofollow",
        link_type_manual=True,
        extra={"condition": "approved"},
    )
    offers.set_working_price(price)
    return price


def edit(client: Client, price: SitePrice, **values: object) -> Any:
    data = {"offer": price.pk, "latest": price.pk, "amount": "900", "currency": "USD"}
    data.update(values)
    return client.post(
        reverse("admin:sites_site_card_price", args=[price.site_id]), data, HTTP_X_SEO_PARTIAL="1"
    )


def test_working_snapshot_and_payment(admin_client: Client, price: SitePrice) -> None:
    placement = Placement.objects.create(
        site=price.site,
        seller=price.seller,
        product=Product.objects.get(domain="convertio.co"),
        price_paid_cents=80000,
        currency="USD",
    )
    response = edit(admin_client, price)
    assert response.status_code == 200
    price.site.refresh_from_db()
    updated = price.site.price
    assert updated is not None and updated.pk != price.pk
    assert updated.placement_cents == 90000 and updated.currency == "USD"
    assert updated.source == MetricSource.MANUAL
    assert updated.link_type == "dofollow" and updated.link_type_manual
    assert (updated.writing_cents, updated.announce_cents, updated.gray_cents) == (12000, 0, 150000)
    assert updated.extra == price.extra and updated.checked_at > price.checked_at
    price.refresh_from_db()
    assert price.placement_cents == 80000
    assert ProductSiteLatest.objects.get(site=price.site).placement_cents == 90000
    placement.refresh_from_db()
    assert placement.price_paid_cents == 80000
    assert price.site.notes.filter(body__contains="$800 →").count() == 1
    assert "$900" in response.content.decode()


def test_other_seller_does_not_change_working(admin_client: Client, price: SitePrice) -> None:
    other = SitePrice.objects.create(
        site=price.site, seller=Seller.collaborator(), placement_cents=70000
    )
    assert edit(admin_client, other, currency="EUR").status_code == 200
    price.site.refresh_from_db()
    assert price.site.price_id == price.pk
    assert SitePrice.objects.filter(seller=other.seller, placement_cents=90000).exists()
    assert price.site.notes.filter(body__startswith="Цена предложения:").exists()


@pytest.mark.parametrize("amount", ["900,50", "900.50", "0"])
def test_valid_amounts(admin_client: Client, price: SitePrice, amount: str) -> None:
    assert edit(admin_client, price, amount=amount).status_code == 200
    price.site.refresh_from_db()
    assert price.site.price is not None
    assert price.site.price.placement_cents == (0 if amount == "0" else 90050)


@pytest.mark.parametrize(
    "values",
    [
        {"amount": ""},
        {"amount": "-1"},
        {"amount": "1.123"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "21474836.48"},
        {"currency": "BAD"},
        {"currency": "EUR"},
    ],
)
def test_invalid_preserves_input_and_snapshot(
    admin_client: Client, price: SitePrice, values: dict[str, str]
) -> None:
    before = SitePrice.objects.count()
    response = edit(admin_client, price, **values)
    assert response.status_code == 200 and b"seo-price-error" in response.content
    assert SitePrice.objects.count() == before
    price.site.refresh_from_db()
    assert price.site.price_id == price.pk


def test_unchanged_no_snapshot(admin_client: Client, price: SitePrice) -> None:
    before = SitePrice.objects.count()
    assert edit(admin_client, price, amount="800").status_code == 200
    assert SitePrice.objects.count() == before


def test_stale_submission_does_not_overwrite(admin_client: Client, price: SitePrice) -> None:
    edit(admin_client, price)
    before = SitePrice.objects.count()
    response = edit(admin_client, price, amount="950")
    assert "Обновите карточку" in response.content.decode()
    assert SitePrice.objects.count() == before


def test_old_working_with_current_offer(admin_client: Client, price: SitePrice) -> None:
    newer = SitePrice.objects.create(
        site=price.site, seller=price.seller, placement_cents=85000, currency="USD"
    )
    assert edit(admin_client, price, latest=newer.pk).status_code == 200
    price.site.refresh_from_db()
    assert price.site.price is not None and price.site.price.placement_cents == 90000


def test_currency_change_without_other_amounts(admin_client: Client, price: SitePrice) -> None:
    price.writing_cents = price.gray_cents = None
    price.save()
    edit(admin_client, price, currency="EUR")
    price.site.refresh_from_db()
    assert price.site.price is not None and price.site.price.currency == "EUR"


def test_permission_and_cross_site(admin_client: Client, client: Client, price: SitePrice) -> None:
    viewer = User.objects.create_user("priceviewer", is_staff=True)
    viewer.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["view_site", "change_site", "view_siteprice", "change_siteprice"]
        )
    )
    client.force_login(viewer)
    assert edit(client, price).status_code == 403
    card = client.get(reverse("admin:sites_site_card", args=[price.site_id]))
    # Цену размещения без права добавлять предложения не править; написание и
    # анонс правятся на месте — им хватает права менять предложение (ADR-071).
    assert 'title="Изменить цену"'.encode() not in card.content
    assert "Изменить цену: написание".encode() in card.content
    other = Site.objects.create(domain="wrong.example")
    url = reverse("admin:sites_site_card_price", args=[other.pk])
    assert (
        admin_client.post(
            url, {"offer": price.pk, "latest": price.pk, "amount": "900", "currency": "USD"}
        ).status_code
        == 404
    )
    assert admin_client.get(url).status_code == 405
