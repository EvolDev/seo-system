"""Продавцы, предложения, рабочая цена и её разбор (E1-07, ADR-043).

Представления (`v_site_offers`, `v_site_latest`, `v_product_site_latest`) —
на данных; текст их сверяет `test_schema_parity`. Правила смены рабочей
цены — `apps/sites/offers.py`. Экраны — `test_offers_admin.py`.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection

from apps.content.models import DomainSetting
from apps.sites import offers
from apps.sites.models import (
    ExchangeRate,
    PlacementType,
    Product,
    Seller,
    Site,
    SiteMetric,
    SiteNote,
    SitePrice,
)

pytestmark = pytest.mark.django_db

OfferFactory = Callable[..., SitePrice]
USD_RATE = Decimal("1.1355")


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def collaborator() -> Seller:
    return Seller.collaborator()


@pytest.fixture
def linkhub() -> Seller:
    return Seller.objects.create(name="LinkHub Media", currency="USD")


@pytest.fixture
def starlinks() -> Seller:
    return Seller.objects.create(name="Starlinks", currency="EUR")


@pytest.fixture
def usd_rate() -> ExchangeRate:
    return ExchangeRate.objects.create(
        currency="USD", rate_date=dt.date(2026, 9, 30), rate=USD_RATE
    )


def _latest(site: Site) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM v_site_latest WHERE id = %s", [site.pk])
        names = [column.name for column in cursor.description or []]
        row = cursor.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def _product_row(site: Site, product: Product) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM v_product_site_latest WHERE site_id = %s AND product_id = %s",
            [site.pk, product.pk],
        )
        names = [column.name for column in cursor.description or []]
        row = cursor.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


class TestSeller:
    def test_collaborator_comes_from_migration(self, collaborator: Seller) -> None:
        assert (collaborator.name, collaborator.currency) == ("Collaborator", "EUR")
        assert collaborator.metrics_trusted is True

    def test_name_unique_ignoring_case(self, linkhub: Seller) -> None:
        duplicate = Seller(name="linkhub media")
        with pytest.raises(ValidationError, match="уже есть"):
            duplicate.full_clean()

    def test_name_spaces_collapsed(self) -> None:
        seller = Seller.objects.create(name="  Athena   Smith ", currency="usd")
        assert (seller.name, seller.currency) == ("Athena Smith", "USD")


class TestEuro:
    """Сравнение в евро — по последнему курсу ЕЦБ; исходная цена хранится."""

    def test_dollars_converted_by_latest_rate(
        self, site: Site, linkhub: Seller, offer: OfferFactory, usd_rate: ExchangeRate
    ) -> None:
        ExchangeRate.objects.create(
            currency="USD", rate_date=dt.date(2026, 9, 1), rate=Decimal("1.0")
        )
        offer(site, 21000, seller=linkhub, currency="USD")
        row = _latest(site)
        assert (row["placement_cents"], row["price_currency"]) == (21000, "USD")
        assert row["placement_eur_cents"] == round(21000 / USD_RATE)

    def test_no_rate_for_today_takes_last_known(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        # Курс за пятницу, сегодня выходной — берётся он.
        ExchangeRate.objects.create(
            currency="USD", rate_date=dt.date(2026, 9, 25), rate=Decimal("1.25")
        )
        offer(site, 25000, seller=linkhub, currency="USD")
        assert _latest(site)["placement_eur_cents"] == 20000

    def test_no_rate_at_all_euros_empty(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 25000, seller=linkhub, currency="USD")
        row = _latest(site)
        assert (row["placement_eur_cents"], row["reference_total_cents"]) == (None, None)


class TestMarks:
    """Пометки «новая цена» и «дешевле» — только по цене услуги одной услуги."""

    def test_cheaper_at_other_seller(
        self,
        site: Site,
        linkhub: Seller,
        starlinks: Seller,
        offer: OfferFactory,
        usd_rate: ExchangeRate,
    ) -> None:
        offer(site, 20000, writing_cents=1000)
        offer(site, 17000, seller=linkhub, currency="USD", working=False)
        offer(site, 18000, seller=starlinks, working=False)
        row = _latest(site)
        # $170 ≈ €149.71 — дешевле €180 у Starlinks и €200 у Collaborator.
        assert (row["cheaper_seller"], row["cheaper_cents"]) == ("LinkHub Media", 17000)
        assert row["cheaper_eur_cents"] == round(17000 / USD_RATE)
        assert row["cheaper_pending"] is True
        assert row["offers_pending"] is True

    def test_more_expensive_and_other_service_are_not_cheaper(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 20000)
        offer(site, 21000, seller=linkhub, currency="EUR", working=False)
        offer(
            site,
            5000,
            seller=linkhub,
            currency="EUR",
            placement_type=PlacementType.LINK_INSERTION,
            working=False,
        )
        assert _latest(site)["cheaper_id"] is None

    def test_new_price_of_same_seller(self, site: Site, offer: OfferFactory) -> None:
        offer(site, 20000, checked_at=dt.datetime(2026, 9, 27, tzinfo=dt.UTC))
        new = offer(site, 22000, working=False, checked_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        row = _latest(site)
        assert (row["new_price_id"], row["new_price_cents"]) == (new.pk, 22000)
        assert row["new_price_pending"] is True

    def test_old_price_of_same_seller_is_not_new(self, site: Site, offer: OfferFactory) -> None:
        offer(site, 20000, checked_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        offer(site, 18000, working=False, checked_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
        assert _latest(site)["new_price_id"] is None

    def test_reviewed_marks_are_not_pending(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 20000)
        cheaper = offer(site, 15000, seller=linkhub, currency="EUR", working=False)
        offers.keep_current([cheaper])
        row = _latest(site)
        assert (row["cheaper_id"], row["cheaper_pending"]) == (cheaper.pk, False)
        assert row["offers_pending"] is False


class TestTrustedMetrics:
    """Метрики продавца — только если нет доверенных замеров (ADR-043)."""

    def test_seller_metrics_when_no_trusted(self, site: Site, linkhub: Seller) -> None:
        SiteMetric.objects.create(site=site, dr=62, seller=linkhub)
        row = _latest(site)
        assert (row["dr"], row["metrics_trusted"], row["metrics_seller"]) == (
            62,
            False,
            "LinkHub Media",
        )

    def test_trusted_wins_even_if_older(self, site: Site, linkhub: Seller) -> None:
        SiteMetric.objects.create(
            site=site, dr=45, checked_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        )
        SiteMetric.objects.create(
            site=site, dr=62, seller=linkhub, checked_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        )
        row = _latest(site)
        assert (row["dr"], row["metrics_trusted"], row["metrics_seller"]) == (45, True, None)

    def test_trusted_seller_counts_as_ours(self, site: Site, collaborator: Seller) -> None:
        SiteMetric.objects.create(site=site, dr=50, seller=collaborator)
        assert _latest(site)["metrics_trusted"] is True


class TestProductView:
    def test_link_insertion_has_no_we_write(
        self, site: Site, convertio: Product, offer: OfferFactory
    ) -> None:
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        offer(site, 15000, placement_type=PlacementType.LINK_INSERTION)
        row = _product_row(site, convertio)
        assert (row["we_write"], row["expected_spend_cents"]) == (None, 15000)

    def test_notes_count_and_last(self, site: Site, convertio: Product) -> None:
        offers.add_note(site, "первая")
        offers.add_note(site, "вторая")
        row = _product_row(site, convertio)
        assert (row["notes_count"], row["last_note"]) == (2, "вторая")


class TestWorkingPrice:
    def test_set_working_price_writes_history(
        self, site: Site, linkhub: Seller, offer: OfferFactory, admin_user: User
    ) -> None:
        offer(site, 24000)
        cheaper = offer(site, 21000, seller=linkhub, currency="USD", working=False)
        assert offers.set_working_price(cheaper, author=admin_user)
        site.refresh_from_db()
        assert site.price_id == cheaper.pk
        note = SiteNote.objects.get(site=site)
        assert note.body == (
            "Цена: Collaborator · публикация €240 → LinkHub Media · публикация $210"
        )
        assert note.author == admin_user

    def test_same_offer_again_changes_nothing(self, site: Site, offer: OfferFactory) -> None:
        working = offer(site, 24000)
        assert not offers.set_working_price(working)
        assert not SiteNote.objects.exists()

    def test_fixing_reviews_same_service_only(
        self, site: Site, linkhub: Seller, starlinks: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 24000)
        chosen = offer(site, 20000, seller=linkhub, currency="EUR", working=False)
        other = offer(site, 22000, seller=starlinks, working=False)
        insertion = offer(
            site, 9000, seller=starlinks, placement_type=PlacementType.LINK_INSERTION, working=False
        )
        offers.set_working_price(chosen)
        pending = set(
            SitePrice.objects.filter(reviewed_at__isnull=True).values_list("pk", flat=True)
        )
        assert pending == {insertion.pk}
        assert other.pk not in pending

    def test_accept_new_prices(self, site: Site, offer: OfferFactory) -> None:
        offer(site, 20000, checked_at=dt.datetime(2026, 9, 27, tzinfo=dt.UTC))
        new = offer(site, 22000, working=False, checked_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
        other = Site.objects.create(domain="other.com")
        offer(other, 10000)
        result = offers.accept_new_prices([site.pk, other.pk])
        assert (result.changed, result.skipped) == (1, 1)
        site.refresh_from_db()
        assert site.price_id == new.pk

    def test_fix_seller_prefers_same_service(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 24000)
        insertion = offer(
            site, 9000, seller=linkhub, placement_type=PlacementType.LINK_INSERTION, working=False
        )
        publication = offer(site, 20000, seller=linkhub, working=False)
        assert offers.fix_seller([site.pk], linkhub) == offers.Outcome(1, 0)
        site.refresh_from_db()
        assert site.price_id == publication.pk
        assert insertion.pk != site.price_id

    def test_fix_seller_without_offer_is_skipped(
        self, site: Site, linkhub: Seller, offer: OfferFactory
    ) -> None:
        offer(site, 24000)
        assert offers.fix_seller([site.pk], linkhub) == offers.Outcome(0, 1)

    def test_keep_current_for_sites(self, site: Site, linkhub: Seller, offer: OfferFactory) -> None:
        working = offer(site, 24000)
        offer(site, 20000, seller=linkhub, working=False)
        assert offers.keep_current_for_sites([site.pk]) == 1
        site.refresh_from_db()
        assert site.price_id == working.pk
        assert not SitePrice.objects.filter(reviewed_at__isnull=True).exists()


class TestMoney:
    @pytest.mark.parametrize(
        ("cents", "currency", "text"),
        [
            (24000, "EUR", "€240"),
            (24050, "EUR", "€240.50"),
            (21000, "USD", "$210"),
            (900, "PLN", "9 PLN"),
        ],
    )
    def test_money(self, cents: int, currency: str, text: str) -> None:
        assert offers.money(cents, currency) == text
