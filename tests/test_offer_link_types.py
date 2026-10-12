"""Тип ссылки принадлежит продавцу; ручные решения переживают импорт."""

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements.forms import PlacementForm
from apps.placements.models import Placement
from apps.sites import offers, sellers
from apps.sites.models import (
    PlacementType,
    Product,
    ProductSiteLatest,
    Seller,
    Site,
    SitePrice,
    Upload,
    UploadKind,
)
from apps.sites.uploads.apply import Writer
from apps.sites.uploads.plan import build_plan
from apps.sites.uploads.records import Parsed, Record, merge_duplicates

pytestmark = pytest.mark.django_db


@pytest.fixture
def pair() -> tuple[Site, SitePrice, SitePrice]:
    Product.objects.get_or_create(domain="convertio.co", defaults={"name": "Convertio"})
    site = Site.objects.create(domain="types.example", link_type="nofollow")
    first = SitePrice.objects.create(
        site=site,
        seller=Seller.objects.create(name="DF seller"),
        placement_cents=10000,
        link_type="dofollow",
    )
    second = SitePrice.objects.create(
        site=site,
        seller=Seller.objects.create(name="NF seller"),
        placement_cents=9000,
        link_type="nofollow",
    )
    offers.set_working_price(first)
    return site, first, second


def test_working_type_follows_selected_seller(pair: tuple[Site, SitePrice, SitePrice]) -> None:
    site, first, second = pair
    assert ProductSiteLatest.objects.get(site=site).link_type == first.link_type
    offers.set_working_price(second)
    assert ProductSiteLatest.objects.get(site=site).link_type == second.link_type
    second.link_type = None
    second.save(update_fields=["link_type"])
    assert ProductSiteLatest.objects.get(site=site).link_type is None


@pytest.mark.parametrize("value", ["dofollow", "nofollow", ""])
def test_card_autosave(
    admin_client: Client, pair: tuple[Site, SitePrice, SitePrice], value: str
) -> None:
    site, first, second = pair
    response = admin_client.post(
        reverse("admin:sites_site_card_link_type", args=[site.pk]),
        {"offer": first.pk, "link_type": value},
        HTTP_X_SEO_PARTIAL="1",
    )
    assert response.status_code in (200, 302)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.link_type == (value or None)
    assert first.link_type_manual
    assert second.link_type == "nofollow"


def test_card_rejects_wrong_site_and_type(
    admin_client: Client, pair: tuple[Site, SitePrice, SitePrice]
) -> None:
    site, first, _ = pair
    url = reverse("admin:sites_site_card_link_type", args=[site.pk])
    assert admin_client.post(url, {"offer": first.pk, "link_type": "sponsored"}).status_code == 400
    other = Site.objects.create(domain="else.example")
    assert (
        admin_client.post(
            reverse("admin:sites_site_card_link_type", args=[other.pk]),
            {"offer": first.pk, "link_type": "nofollow"},
        ).status_code
        == 404
    )
    assert admin_client.get(url).status_code == 405


def test_readonly_user_cannot_save(
    client: Client, django_user_model: type[User], pair: tuple[Site, SitePrice, SitePrice]
) -> None:
    site, first, _ = pair
    user = django_user_model.objects.create_user("viewer", is_staff=True)
    client.force_login(user)
    response = client.post(
        reverse("admin:sites_site_card_link_type", args=[site.pk]),
        {"offer": first.pk, "link_type": "nofollow"},
    )
    assert response.status_code == 403


def _write(
    seller: Seller,
    value: str | None,
    day: dt.date,
    service: PlacementType = PlacementType.GUEST_POST,
) -> SitePrice:
    record = Record(line=2, domain="types.example", offers={service: 10000}, link_type=value)
    parsed = Parsed([record], [], [])
    plan = build_plan(
        parsed, seller_id=seller.pk, currency="EUR", prices_date=day, rates={}, recheck_pct=10
    )
    upload = Upload.objects.create(
        kind=UploadKind.PRICE_LIST,
        seller=seller,
        prices_date=day,
        file_name="types.csv",
        file_path="types.csv",
        file_sha256=str(day),
    )
    writer = Writer(upload, plan)
    writer.run()
    return SitePrice.objects.filter(
        site__domain="types.example", seller=seller, placement_type=service
    ).latest("checked_at", "pk")


@pytest.mark.parametrize("manual", ["nofollow", None])
def test_import_keeps_manual_override(
    pair: tuple[Site, SitePrice, SitePrice], manual: str | None
) -> None:
    _, first, second = pair
    offers.set_link_type(first, manual)
    imported = _write(first.seller, "dofollow", timezone.localdate() + dt.timedelta(days=1))
    assert imported.link_type == manual
    assert imported.link_type_manual
    other = _write(second.seller, "dofollow", timezone.localdate() + dt.timedelta(days=2))
    assert other.link_type == "dofollow"
    assert not other.link_type_manual
    insertion = _write(
        first.seller,
        "dofollow",
        timezone.localdate() + dt.timedelta(days=2),
        PlacementType.LINK_INSERTION,
    )
    assert insertion.link_type == "dofollow"


def test_import_unknown_and_known_types(pair: tuple[Site, SitePrice, SitePrice]) -> None:
    _, first, _ = pair
    imported = _write(first.seller, "nofollow", timezone.localdate() + dt.timedelta(days=1))
    assert imported.link_type == "nofollow"
    assert not imported.link_type_manual
    imported = _write(first.seller, None, timezone.localdate() + dt.timedelta(days=2))
    assert imported.link_type == "nofollow"


def test_placement_snapshot_does_not_follow_other_seller(
    pair: tuple[Site, SitePrice, SitePrice],
) -> None:
    site, first, second = pair
    placement = Placement.objects.create(
        site=site,
        product=Product.objects.get(domain="convertio.co"),
        seller=first.seller,
        placement_type=first.placement_type,
    )
    assert placement.link_type == "dofollow"
    offers.set_working_price(second)
    placement.refresh_from_db()
    assert placement.link_type == "dofollow"
    wrong = Placement.objects.create(
        site=site,
        product=placement.product,
        seller=first.seller,
        placement_type=first.placement_type,
    )
    assert wrong.link_type is None


def test_placement_blank_is_manual(pair: tuple[Site, SitePrice, SitePrice]) -> None:
    site, first, _ = pair
    form = PlacementForm(
        data={
            "site": site.pk,
            "product": Product.objects.get(domain="convertio.co").pk,
            "seller": first.seller_id,
            "placement_type": first.placement_type,
            "status": "in_work",
            "link_type": "",
            "currency": "EUR",
        }
    )
    form.fields.pop("created_at")
    assert form.is_valid(), form.errors
    placement = form.save()
    assert placement.link_type is None
    assert placement.link_type_manual


def test_site_filters_use_working_seller(
    admin_client: Client, pair: tuple[Site, SitePrice, SitePrice]
) -> None:
    site, _, second = pair
    url = reverse("admin:sites_productsitelatest_changelist")
    response = admin_client.get(url, {"link_type": "dofollow"})
    assert site.domain in response.content.decode()
    response = admin_client.get(url, {"link_type": "nofollow"})
    assert site.domain not in response.content.decode()
    offers.set_working_price(second)
    response = admin_client.get(url, {"link_type": "nofollow"})
    assert site.domain in response.content.decode()


def test_duplicate_rows_keep_type_of_selected_service() -> None:
    records, _ = merge_duplicates(
        [
            Record(
                line=2,
                domain="duplicates.example",
                offers={PlacementType.GUEST_POST: 15000},
                link_type="nofollow",
            ),
            Record(
                line=3,
                domain="duplicates.example",
                offers={PlacementType.GUEST_POST: 10000},
                link_type="dofollow",
            ),
            Record(
                line=4,
                domain="duplicates.example",
                offers={PlacementType.LINK_INSERTION: 8000},
                link_type="nofollow",
            ),
        ]
    )
    record = records[0]
    assert record.offers[PlacementType.GUEST_POST] == 10000
    assert record.link_types == {
        PlacementType.GUEST_POST: "dofollow",
        PlacementType.LINK_INSERTION: "nofollow",
    }
    plan = build_plan(
        Parsed(records, [], []),
        seller_id=Seller.collaborator().pk,
        currency="EUR",
        prices_date=timezone.localdate(),
        rates={},
        recheck_pct=10,
    )
    upload = Upload.objects.create(
        kind=UploadKind.PRICE_LIST,
        seller=Seller.collaborator(),
        prices_date=timezone.localdate(),
        file_name="duplicates.csv",
        file_path="duplicates.csv",
        file_sha256="duplicates",
    )
    Writer(upload, plan).run()
    assert (
        dict(
            SitePrice.objects.filter(site__domain=record.domain).values_list(
                "placement_type", "link_type"
            )
        )
        == record.link_types
    )


def test_seller_merge_preserves_different_link_types() -> None:
    site = Site.objects.create(domain="merge-types.example")
    target = Seller.objects.create(name="Main")
    source = Seller.objects.create(name="Duplicate name")
    day = timezone.now()
    for seller, value in [(target, "dofollow"), (source, "nofollow")]:
        SitePrice.objects.create(
            site=site, seller=seller, checked_at=day, placement_cents=10000, link_type=value
        )
    report = sellers.merge(target, [source])
    assert report.duplicates == 0
    assert set(SitePrice.objects.filter(site=site).values_list("link_type", flat=True)) == {
        "dofollow",
        "nofollow",
    }


@pytest.mark.parametrize("manual", ["nofollow", None])
def test_manual_working_type_survives_newer_pending_offer(
    pair: tuple[Site, SitePrice, SitePrice], manual: str | None
) -> None:
    _, working, _ = pair
    pending = SitePrice.objects.create(
        site=working.site,
        seller=working.seller,
        placement_cents=14000,
        checked_at=timezone.now() + dt.timedelta(days=1),
        link_type="dofollow",
    )
    offers.set_link_type(working, manual)
    pending.refresh_from_db()
    assert pending.link_type == manual and pending.link_type_manual
    imported = _write(working.seller, "dofollow", timezone.localdate() + dt.timedelta(days=2))
    assert imported.link_type == manual and imported.link_type_manual
