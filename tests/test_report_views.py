"""Отчётные представления: что считают и по каким правилам (E1-05).

Текст представлений сверяет `test_schema_parity`, здесь — поведение на данных.
`v_overdue_checks` — в `test_observability_models` (E1-03).
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection

from apps.content.models import DomainSetting
from apps.keywords.models import Keyword, KeywordPosition
from apps.observability.models import ApiUsage, LlmCall, LlmStatus
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import (
    AuditAuthor,
    AuditVerdict,
    GrayScan,
    Product,
    ProductSite,
    Site,
    SiteAudit,
    SiteMetric,
    SitePrice,
    SiteStatus,
)

pytestmark = pytest.mark.django_db

OfferFactory = Callable[..., SitePrice]

EARLY = datetime(2026, 8, 1, tzinfo=UTC)
LATE = datetime(2026, 9, 1, tzinfo=UTC)

WAITING = [
    PlacementStatus.IN_WORK,
    PlacementStatus.ORDERED,
    PlacementStatus.WRITING,
    PlacementStatus.WRITING,
]


def _rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        names = [column.name for column in cursor.description or []]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _one(sql: str, *params: Any) -> dict[str, Any]:
    rows = _rows(sql, *params)
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


def _placement(site: Site, product: Product, status: str, **fields: Any) -> Placement:
    return Placement.objects.create(site=site, product=product, status=status, **fields)


def _link(placement: Placement, keyword: Keyword | None = None) -> PlacementLink:
    return PlacementLink.objects.create(
        placement=placement, keyword=keyword, anchor="convert", target_url="https://convertio.co/"
    )


class TestKeywordCoverage:
    @pytest.fixture
    def keyword(self, convertio: Product) -> Keyword:
        return Keyword.objects.create(
            product=convertio, keyword="convert", target_url="https://convertio.co/"
        )

    def _coverage(self, keyword: Keyword) -> dict[str, Any]:
        return _one("SELECT * FROM v_keyword_coverage WHERE id = %s", keyword.pk)

    def test_placed_only_published_waiting_four_statuses(
        self, keyword: Keyword, convertio: Product, site: Site
    ) -> None:
        for status in WAITING:
            _link(_placement(site, convertio, status), keyword)
        published = _placement(site, convertio, PlacementStatus.PLACED)
        _link(published, keyword)
        _link(published, keyword)
        for status in (PlacementStatus.REJECTED, PlacementStatus.REJECTED):
            _link(_placement(site, convertio, status), keyword)

        row = self._coverage(keyword)
        assert (row["links_placed"], row["links_waiting"]) == (2, 4)

    def test_links_of_other_keyword_and_without_keyword_not_counted(
        self, keyword: Keyword, convertio: Product, site: Site
    ) -> None:
        other = Keyword.objects.create(
            product=convertio, keyword="video converter", target_url="https://convertio.co/video/"
        )
        placement = _placement(site, convertio, PlacementStatus.PLACED)
        _link(placement, other)
        _link(placement)

        row = self._coverage(keyword)
        assert (row["links_placed"], row["links_waiting"]) == (0, 0)

    def test_inactive_keyword_is_hidden(self, keyword: Keyword) -> None:
        Keyword.objects.filter(pk=keyword.pk).update(is_active=False)
        assert _rows("SELECT * FROM v_keyword_coverage WHERE id = %s", keyword.pk) == []

    def test_last_position_is_latest_in_us(self, keyword: Keyword) -> None:
        KeywordPosition.objects.create(keyword=keyword, position=12, checked_at=date(2026, 8, 1))
        KeywordPosition.objects.create(keyword=keyword, position=7, checked_at=date(2026, 9, 1))
        KeywordPosition.objects.create(
            keyword=keyword, position=3, country="DE", checked_at=date(2026, 9, 15)
        )
        assert self._coverage(keyword)["last_position"] == 7


class TestSiteLatest:
    def _latest(self, site: Site) -> dict[str, Any]:
        return _one("SELECT * FROM v_site_latest WHERE id = %s", site.pk)

    def test_latest_snapshots_and_working_price(self, site: Site, offer: OfferFactory) -> None:
        SiteMetric.objects.create(site=site, dr=40, checked_at=EARLY)
        SiteMetric.objects.create(site=site, dr=55, checked_at=LATE)
        # Цена — рабочая, а не последняя по дате (ADR-043).
        offer(site, 30000, checked_at=EARLY)
        offer(site, 20000, checked_at=LATE, working=False)
        GrayScan.objects.create(site=site, ratio="12.50", checked_at=LATE)

        row = self._latest(site)
        assert (row["dr"], row["placement_cents"], str(row["gray_ratio"])) == (55, 30000, "12.50")

    def test_reference_total_is_placement_plus_announce_without_writing(
        self, site: Site, offer: OfferFactory
    ) -> None:
        offer(site, 40000, announce_cents=10000, writing_cents=5000)
        assert self._latest(site)["reference_total_cents"] == 50000

    def test_without_working_price_reference_total_is_empty(self, site: Site) -> None:
        # Раньше было 0, будто бесплатно (долг из PROGRESS, закрыт в E1-07).
        assert self._latest(site)["reference_total_cents"] is None

    def test_deleted_site_is_hidden(self, site: Site) -> None:
        Site.all_objects.filter(pk=site.pk).update(is_deleted=True)
        assert _rows("SELECT * FROM v_site_latest WHERE id = %s", site.pk) == []


class TestProductSiteLatest:
    """`we_write` и `expected_spend` — по порогу написания продукта (ADR-009)."""

    def _row(self, site: Site, product: Product) -> dict[str, Any]:
        return _one(
            "SELECT * FROM v_product_site_latest WHERE site_id = %s AND product_id = %s",
            site.pk,
            product.pk,
        )

    @pytest.fixture(autouse=True)
    def _offers(self, offer: OfferFactory) -> None:
        self.offer = offer

    def _price(self, site: Site, writing_cents: int | None) -> None:
        self.offer(site, 40000, announce_cents=10000, writing_cents=writing_cents)

    def _threshold(self, writing_eur: int, product: Product | None = None) -> None:
        DomainSetting.objects.create(
            key="PRICE_REFERENCE",
            product=product,
            value={"total_eur": 550, "writing_eur": writing_eur, "announce_eur": 100},
        )

    def test_without_threshold_spend_is_empty(self, site: Site, convertio: Product) -> None:
        self._price(site, 4000)
        row = self._row(site, convertio)
        # Порог не угадываем: кто пишет — неизвестно.
        assert (row["we_write"], row["expected_spend_cents"]) == (None, None)

    def test_writing_within_threshold_platform_writes(self, site: Site, convertio: Product) -> None:
        self._threshold(50, convertio)
        self._price(site, 5000)
        row = self._row(site, convertio)
        assert (row["we_write"], row["expected_spend_cents"]) == (False, 55000)

    def test_writing_above_threshold_we_write(self, site: Site, convertio: Product) -> None:
        self._threshold(50, convertio)
        self._price(site, 6000)
        row = self._row(site, convertio)
        assert (row["we_write"], row["expected_spend_cents"]) == (True, 50000)

    def test_unknown_writing_price_we_write(self, site: Site, convertio: Product) -> None:
        self._threshold(50, convertio)
        self._price(site, None)
        row = self._row(site, convertio)
        assert (row["we_write"], row["expected_spend_cents"]) == (True, 50000)

    def test_general_threshold_applies_local_overrides(
        self, site: Site, convertio: Product, clideo: Product
    ) -> None:
        self._threshold(50)
        self._threshold(30, clideo)
        self._price(site, 4000)
        assert self._row(site, convertio)["we_write"] is False
        assert self._row(site, clideo)["we_write"] is True

    def test_threshold_of_other_product_does_not_apply(
        self, site: Site, convertio: Product, clideo: Product
    ) -> None:
        self._threshold(50, clideo)
        self._price(site, 4000)
        assert self._row(site, convertio)["expected_spend_cents"] is None

    def test_status_audit_and_placements_of_this_product(
        self, site: Site, convertio: Product, clideo: Product
    ) -> None:
        SiteAudit.objects.create(
            site=site,
            product=convertio,
            verdict=AuditVerdict.NO,
            author=AuditAuthor.HUMAN,
            created_at=EARLY,
        )
        SiteAudit.objects.create(
            site=site,
            product=convertio,
            verdict=AuditVerdict.YES,
            score=80,
            author=AuditAuthor.HUMAN,
            created_at=LATE,
        )
        SiteAudit.objects.create(
            site=site,
            product=clideo,
            verdict=AuditVerdict.NO,
            author=AuditAuthor.HUMAN,
            created_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
        _placement(site, convertio, PlacementStatus.PLACED)
        _placement(site, convertio, PlacementStatus.ORDERED)
        _placement(site, clideo, PlacementStatus.PLACED)
        # Статус — тот, что в решении по продукту, даже если человек поставил его
        # руками после публикации (сама публикация ставит «Размещались»).
        ProductSite.objects.filter(site=site, product=convertio).update(status=SiteStatus.IN_WORK)

        row = self._row(site, convertio)
        assert row["status"] == SiteStatus.IN_WORK
        assert (row["last_verdict"], row["last_score"]) == (AuditVerdict.YES, 80)
        assert row["placements_published"] == 1
        assert row["other_products"] == ["Clideo"]

        clideo_row = self._row(site, clideo)
        assert clideo_row["last_verdict"] == AuditVerdict.NO
        assert clideo_row["other_products"] == ["Convertio"]

    def test_no_other_products_is_empty(self, site: Site, convertio: Product) -> None:
        assert self._row(site, convertio)["other_products"] is None


class TestSiteFunnel:
    def test_counts_by_product_status_and_undecided(
        self, convertio: Product, clideo: Product
    ) -> None:
        first = Site.objects.create(domain="first.com")
        Site.objects.create(domain="second.com")
        deleted = Site.objects.create(domain="deleted.com")
        ProductSite.objects.filter(site=first, product=convertio).update(status=SiteStatus.IN_WORK)
        ProductSite.objects.filter(site__domain="second.com", product=convertio).update(
            imported_undecided=True
        )
        Site.all_objects.filter(pk=deleted.pk).update(is_deleted=True)

        rows = _rows(
            "SELECT status, imported_undecided, sites FROM v_site_funnel"
            " WHERE product_id = %s ORDER BY status, imported_undecided",
            convertio.pk,
        )
        assert rows == [
            {"status": SiteStatus.NEW, "imported_undecided": True, "sites": 1},
            {"status": SiteStatus.IN_WORK, "imported_undecided": False, "sites": 1},
        ]
        assert _one(
            "SELECT sum(sites)::int AS total FROM v_site_funnel WHERE product_id = %s", clideo.pk
        ) == {"total": 2}


class TestLinkHealth:
    def test_only_published_placements(self, site: Site, convertio: Product) -> None:
        published = _placement(site, convertio, PlacementStatus.PLACED, published_at=EARLY)
        _link(published)
        _link(_placement(site, convertio, PlacementStatus.ORDERED))

        rows = _rows("SELECT domain, placement_id, age FROM v_link_health")
        assert [(row["domain"], row["placement_id"]) for row in rows] == [
            ("example.com", published.pk)
        ]
        assert rows[0]["age"].days > 0


class TestMonthlySpend:
    def test_sums_by_month_item_and_currency(self, site: Site, convertio: Product) -> None:
        # Запрос к выдаче стоит доли цента (ADR-040): сумма не теряет их.
        ApiUsage.objects.create(provider="serper", cost_cents=Decimal("0.1"), created_at=EARLY)
        ApiUsage.objects.create(provider="serper", cost_cents=Decimal("0.06"), created_at=EARLY)
        LlmCall.objects.create(
            task="audit",
            model="claude-opus-5-5",
            cost_cents=300,
            status=LlmStatus.OK,
            created_at=LATE,
        )
        _placement(
            site,
            convertio,
            PlacementStatus.PLACED,
            published_at=LATE,
            price_paid_cents=45000,
        )
        _placement(
            site,
            convertio,
            PlacementStatus.PLACED,
            published_at=LATE,
            price_paid_cents=5000,
            currency="USD",
        )
        _placement(site, convertio, PlacementStatus.ORDERED, price_paid_cents=99900)

        rows = _rows(
            "SELECT month, item, currency, cost_cents FROM v_monthly_spend"
            " ORDER BY month, item, currency"
        )
        assert [tuple(row.values()) for row in rows] == [
            (date(2026, 8, 1), "serper", "USD", Decimal("0.16")),
            (date(2026, 9, 1), "llm:claude-opus-5-5", "USD", 300),
            (date(2026, 9, 1), "placements", "EUR", 45000),
            (date(2026, 9, 1), "placements", "USD", 5000),
        ]
