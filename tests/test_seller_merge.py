"""Слияние продавцов: дубли из выгрузок сводятся в одного (E1-17).

Главное, что проверяется: после слияния ничего не теряется и не рвётся —
рабочая цена площадки, строки разбора загрузок, счета и размещения остаются
на месте, у них лишь меняется продавец.
"""

import datetime as dt

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements.models import Invoice, InvoiceStatus, Placement, PlacementStatus
from apps.sites import sellers
from apps.sites.models import (
    MetricSource,
    Product,
    ReviewGroup,
    Seller,
    Site,
    SiteMetric,
    SitePrice,
    Upload,
    UploadItem,
    UploadKind,
)
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

DAY = dt.date(2026, 9, 14)


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def pair() -> tuple[Seller, Seller]:
    """Один и тот же человек, записанный в файлах по-разному."""
    return (
        Seller.objects.create(name="Saket Aggarwal", currency="EUR", contacts="saket@mail.com"),
        Seller.objects.create(name="Saket Aggrwal", currency="USD", notes="второй вариант имени"),
    )


def _price(site: Site, seller: Seller, cents: int, day: dt.date = DAY) -> SitePrice:
    return SitePrice.objects.create(
        site=site,
        seller=seller,
        placement_cents=cents,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(day),
        reviewed_at=timezone.now(),
    )


def _working(site: Site, price: SitePrice) -> None:
    site.price = price
    site.save(update_fields=["price"])


def test_everything_moves_to_the_target(pair: tuple[Seller, Seller], product: Product) -> None:
    target, source = pair
    site = Site.objects.create(domain="a.com")
    _price(site, source, 20000)
    SiteMetric.objects.create(site=site, seller=source, dr=40)
    Upload.objects.create(
        kind=UploadKind.PRICE_LIST,
        seller=source,
        file_name="a.csv",
        file_path="a.csv",
        prices_date=DAY,
    )
    placement = Placement.objects.create(site=site, product=product, seller=source)
    invoice = Invoice.objects.create(seller=source, amount_cents=10000, status=InvoiceStatus.ISSUED)

    report = sellers.merge(target, [source])

    assert (report.prices, report.metrics, report.uploads) == (1, 1, 1)
    assert (report.placements, report.invoices) == (1, 1)
    assert SitePrice.objects.get().seller_id == target.pk
    assert SiteMetric.objects.get().seller_id == target.pk
    assert Upload.objects.get().seller_id == target.pk
    placement.refresh_from_db()
    invoice.refresh_from_db()
    assert placement.seller_id == target.pk
    assert invoice.seller_id == target.pk
    assert not Seller.objects.filter(pk=source.pk).exists()


def test_working_price_of_the_site_survives(pair: tuple[Seller, Seller]) -> None:
    target, source = pair
    site = Site.objects.create(domain="a.com")
    price = _price(site, source, 20000)
    _working(site, price)

    sellers.merge(target, [source])

    site.refresh_from_db()
    assert site.price_id == price.pk
    assert site.price is not None
    assert site.price.seller_id == target.pk


def test_same_price_twice_becomes_one(pair: tuple[Seller, Seller], product: Product) -> None:
    """Одна и та же цена под двумя именами — одна строка, ссылки переводятся."""
    target, source = pair
    site = Site.objects.create(domain="a.com")
    kept = _price(site, target, 20000)
    twin = _price(site, source, 20000)
    _working(site, twin)
    upload = Upload.objects.create(
        kind=UploadKind.PLACEMENTS,
        seller=None,
        product=product,  # у загрузки размещений продукт обязателен
        file_name="b.csv",
        file_path="b.csv",
        prices_date=DAY,
    )
    item = UploadItem.objects.create(
        upload=upload,
        site=site,
        price=twin,
        ref_price=twin,
        review_group=ReviewGroup.SAME,
        line=1,
    )

    report = sellers.merge(target, [source])

    assert report.duplicates == 1
    assert SitePrice.objects.count() == 1
    site.refresh_from_db()
    item.refresh_from_db()
    assert site.price_id == kept.pk
    assert (item.price_id, item.ref_price_id) == (kept.pk, kept.pk)


def test_price_on_another_day_is_kept(pair: tuple[Seller, Seller]) -> None:
    target, source = pair
    site = Site.objects.create(domain="a.com")
    _price(site, target, 20000)
    _price(site, source, 20000, day=dt.date(2026, 8, 1))

    report = sellers.merge(target, [source])

    assert report.duplicates == 0
    assert SitePrice.objects.filter(seller=target).count() == 2


def test_collaborator_cannot_be_merged_into_another(pair: tuple[Seller, Seller]) -> None:
    target, _ = pair
    collaborator = Seller.collaborator()
    with pytest.raises(sellers.MergeError, match="Collaborator"):
        sellers.merge(target, [collaborator])
    assert Seller.objects.filter(pk=collaborator.pk).exists()


def test_sources_can_be_kept_empty(pair: tuple[Seller, Seller]) -> None:
    target, source = pair
    site = Site.objects.create(domain="a.com")
    _price(site, source, 20000)

    report = sellers.merge(target, [source], delete_sources=False)

    assert report.deleted == 0
    assert Seller.objects.filter(pk=source.pk).exists()
    assert not SitePrice.objects.filter(seller=source).exists()


def test_contacts_and_notes_are_not_lost(pair: tuple[Seller, Seller]) -> None:
    target, source = pair
    sellers.merge(target, [source], currency="USD")
    target.refresh_from_db()
    assert "Saket Aggrwal" in (target.notes or "")
    assert "второй вариант имени" in (target.notes or "")
    assert target.currency == "USD"


def test_facts_count_what_matters(pair: tuple[Seller, Seller], product: Product) -> None:
    target, source = pair
    site = Site.objects.create(domain="a.com")
    price = _price(site, target, 20000)
    _working(site, price)
    Placement.objects.create(site=site, product=product, seller=target)

    found = {item.seller.name: item for item in sellers.facts([target.pk, source.pk])}

    assert (found["Saket Aggarwal"].prices, found["Saket Aggarwal"].working) == (1, 1)
    assert found["Saket Aggarwal"].placements == 1
    assert found["Saket Aggarwal"].last_price == DAY
    assert found["Saket Aggrwal"].weight == 0


class TestScreen:
    """Экран слияния: действие ведёт на граф, кнопка объединяет."""

    def test_action_opens_the_graph(
        self, admin_client: Client, pair: tuple[Seller, Seller]
    ) -> None:
        target, source = pair
        response = admin_client.post(
            reverse("admin:sites_seller_changelist"),
            {"action": "merge_action", "_selected_action": [str(target.pk), str(source.pk)]},
        )
        assert response.status_code == 302
        page = admin_client.get(response["Location"])
        assert page.status_code == 200
        assert page.content.decode().count("data-node=") == 2

    def test_one_seller_is_not_enough(
        self, admin_client: Client, pair: tuple[Seller, Seller]
    ) -> None:
        target, _ = pair
        response = admin_client.post(
            reverse("admin:sites_seller_changelist"),
            {"action": "merge_action", "_selected_action": [str(target.pk)]},
            follow=True,
        )
        assert "Отметьте хотя бы двоих" in response.content.decode()

    def test_merge_from_the_screen(self, admin_client: Client, pair: tuple[Seller, Seller]) -> None:
        target, source = pair
        site = Site.objects.create(domain="a.com")
        _price(site, source, 20000)
        url = reverse("admin:sites_seller_merge")
        response = admin_client.post(
            f"{url}?id={target.pk}&id={source.pk}", {"target": str(target.pk)}, follow=True
        )
        assert "Объединено в «Saket Aggarwal»" in response.content.decode()
        assert SitePrice.objects.get().seller_id == target.pk
        assert not Seller.objects.filter(pk=source.pk).exists()

    def test_skipped_seller_stays(self, admin_client: Client, pair: tuple[Seller, Seller]) -> None:
        target, source = pair
        third = Seller.objects.create(name="WM Links", currency="EUR")
        url = reverse("admin:sites_seller_merge")
        admin_client.post(
            f"{url}?id={target.pk}&id={source.pk}&id={third.pk}",
            {"target": str(target.pk), "skip": [str(third.pk)]},
            follow=True,
        )
        assert Seller.objects.filter(pk=third.pk).exists()
        assert not Seller.objects.filter(pk=source.pk).exists()


def test_placement_statuses_are_not_touched(pair: tuple[Seller, Seller], product: Product) -> None:
    """Слияние меняет только продавца: статус и «заплачено» размещения не трогает."""
    target, source = pair
    site = Site.objects.create(domain="a.com")
    placement = Placement.objects.create(
        site=site,
        product=product,
        seller=source,
        status=PlacementStatus.PUBLISHED,
        price_paid_cents=15000,
    )

    sellers.merge(target, [source])

    placement.refresh_from_db()
    assert placement.status == PlacementStatus.PUBLISHED
    assert placement.price_paid_cents == 15000
    assert placement.seller_id == target.pk
