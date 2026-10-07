"""Заранее посчитанная «площадка на сегодня» (E1-11, ADR-065).

Копия обязана совпадать с живым подсчётом и обновляться при каждом
источнике изменений: загрузка, импорт, смена рабочей цены, смена курсов.
Главная проверка — `latest.stale()`: она считает теми же правилами и
сравнивает построчно, поэтому ловит любое расхождение, а не только то,
про которое мы вспомнили.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.sites import latest, offers
from apps.sites.models import (
    ExchangeRate,
    MetricSource,
    Seller,
    Site,
    SiteLatest,
    SiteMetric,
    SitePrice,
)
from apps.sites.uploads.plan import start_of_day

pytestmark = pytest.mark.django_db

DAY = dt.date(2026, 10, 1)


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="donor.com")


def _metric(site: Site, dr: int, seller: Seller | None = None, **fields: object) -> SiteMetric:
    return SiteMetric.objects.create(site=site, dr=dr, seller=seller, **fields)


class TestCompute:
    def test_single_metric_refreshes_itself(self, site: Site) -> None:
        """Одиночный замер пересчитывает копию сам — он идёт через `save()`."""
        _metric(site, dr=50)
        assert SiteLatest.objects.get(site=site).dr == 50

    def test_bulk_create_needs_an_explicit_refresh(self, site: Site) -> None:
        """`bulk_create` не зовёт `save()`: пачки пересчитывают копию явно.

        Так пишут загрузка и импорт — там пересчёт стоит после всей записи.
        """
        SiteMetric.objects.bulk_create([SiteMetric(site=site, dr=50)])
        # Строка копии у площадки уже есть — её завёл `Site.save()`, — но
        # замера она не видела: пачка прошла мимо `save()`.
        assert SiteLatest.objects.get(site=site).dr is None
        latest.refresh([site.pk])
        assert SiteLatest.objects.get(site=site).dr == 50

    def test_trusted_metric_wins(self, site: Site) -> None:
        """Наш замер важнее замера со слов продавца (ADR-043)."""
        seller = Seller.objects.create(name="Zain", metrics_trusted=False)
        _metric(site, dr=10, seller=seller, checked_at=timezone.now())
        _metric(site, dr=80, checked_at=timezone.now() - dt.timedelta(days=5))
        latest.refresh([site.pk])
        row = SiteLatest.objects.get(site=site)
        assert (row.dr, row.metrics_trusted) == (80, True)

    def test_geo_is_kept_from_the_measure_that_has_it(self, site: Site) -> None:
        """Замер каталога без гео не стирает топ-регион (ADR-045)."""
        _metric(
            site,
            dr=40,
            top_geo="us",
            top_geo_traffic=900,
            checked_at=timezone.now() - dt.timedelta(days=2),
        )
        _metric(site, dr=42, checked_at=timezone.now())
        latest.refresh([site.pk])
        row = SiteLatest.objects.get(site=site)
        assert (row.dr, row.top_geo, row.top_geo_traffic) == (42, "us", 900)

    def test_refresh_replaces_and_does_not_double(self, site: Site) -> None:
        _metric(site, dr=30)
        latest.refresh([site.pk])
        _metric(site, dr=70, checked_at=timezone.now() + dt.timedelta(minutes=1))
        latest.refresh([site.pk])
        assert SiteLatest.objects.filter(site=site).count() == 1
        assert SiteLatest.objects.get(site=site).dr == 70

    def test_refresh_all_covers_every_site(self) -> None:
        for number in range(3):
            made = Site.objects.create(domain=f"d{number}.com")
            _metric(made, dr=number)
        assert latest.refresh_all() == 3
        assert SiteLatest.objects.count() == 3


class TestAgreement:
    """`stale()` — главная защита: копия против тех же правил на живых данных."""

    def test_fresh_copy_agrees(self, site: Site) -> None:
        _metric(site, dr=55, organic_traffic=1000)
        latest.refresh([site.pk])
        assert latest.stale() == []

    def test_stale_copy_is_found(self, site: Site) -> None:
        _metric(site, dr=55)
        latest.refresh([site.pk])
        SiteLatest.objects.filter(site=site).update(dr=1)
        found = latest.stale()
        assert [row["domain"] for row in found] == ["donor.com"]

    def test_missing_row_is_found(self, site: Site) -> None:
        """Площадка без строки копии — тоже расхождение."""
        SiteMetric.objects.bulk_create([SiteMetric(site=site, dr=55)])
        assert [row["domain"] for row in latest.stale()] == ["donor.com"]


class TestSources:
    def test_working_price_change_refreshes_the_copy(self, site: Site) -> None:
        """Пометка «дешевле» считается от рабочей цены — копия идёт за ней."""
        ExchangeRate.objects.create(currency="USD", rate_date=DAY, rate=Decimal("1.1"))
        mine = Seller.objects.create(name="Alpha")
        other = Seller.objects.create(name="Beta")
        dear = SitePrice.objects.create(
            site=site,
            seller=mine,
            placement_cents=30000,
            currency="EUR",
            source=MetricSource.MANUAL,
            checked_at=start_of_day(DAY),
            reviewed_at=timezone.now(),
        )
        SitePrice.objects.create(
            site=site,
            seller=other,
            placement_cents=10000,
            currency="EUR",
            source=MetricSource.MANUAL,
            checked_at=start_of_day(DAY),
            reviewed_at=timezone.now(),
        )
        site.price = dear
        site.save(update_fields=["price"])
        latest.refresh([site.pk])
        assert SiteLatest.objects.get(site=site).cheaper_cents == 10000

        cheap = SitePrice.objects.get(seller=other)
        offers.set_working_price(cheap)
        # Рабочей стала дешёвая — дешевле больше некуда, пометка снялась сама.
        assert SiteLatest.objects.get(site=site).cheaper_cents is None
        assert latest.stale() == []
