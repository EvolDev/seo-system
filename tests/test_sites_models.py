"""Модели блока 1: строки продукт × площадка, мягкое удаление, база (E1-01)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from apps.sites.models import (
    AuditAuthor,
    AuditVerdict,
    Product,
    ProductSite,
    Site,
    SiteAudit,
    SiteMetric,
    SiteStatus,
    ensure_product_sites,
)

pytestmark = pytest.mark.django_db


def _pairs() -> set[tuple[int, int]]:
    return set(ProductSite.objects.values_list("product_id", "site_id"))


class TestProductSites:
    def test_new_site_gets_row_for_every_product(self) -> None:
        convertio = Product.objects.create(name="Convertio", domain="convertio.co")
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        site = Site.objects.create(domain="example.com")
        assert _pairs() == {(convertio.pk, site.pk), (clideo.pk, site.pk)}

    def test_new_product_gets_row_for_every_site(self) -> None:
        first = Site.objects.create(domain="first.com")
        second = Site.objects.create(domain="second.com")
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        assert _pairs() == {(product.pk, first.pk), (product.pk, second.pk)}

    def test_deleted_site_also_gets_row(self) -> None:
        # Строка есть для каждой пары: удалённую площадку можно восстановить.
        site = Site.objects.create(domain="gone.com", is_deleted=True)
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        assert _pairs() == {(product.pk, site.pk)}

    def test_new_row_is_new_and_decided_by_nobody(self) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        Site.objects.create(domain="example.com")
        row = ProductSite.objects.get()
        assert row.status == SiteStatus.NEW
        assert row.imported_undecided is False

    def test_repeat_call_does_not_duplicate(self) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        Site.objects.create(domain="example.com")
        ensure_product_sites()
        ensure_product_sites()
        assert ProductSite.objects.count() == 1

    def test_repeat_call_keeps_decision(self) -> None:
        Product.objects.create(name="Convertio", domain="convertio.co")
        Site.objects.create(domain="example.com")
        ProductSite.objects.update(status=SiteStatus.REJECTED, reject_reason="nofollow")
        ensure_product_sites()
        row = ProductSite.objects.get()
        assert (row.status, row.reject_reason) == (SiteStatus.REJECTED, "nofollow")

    def test_fills_missing_pairs(self) -> None:
        # Например, после bulk_create площадок, который не вызывает save().
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        Site.objects.bulk_create([Site(domain="a.com"), Site(domain="b.com")])
        assert ProductSite.objects.count() == 0
        ensure_product_sites()
        assert {p for p, _ in _pairs()} == {product.pk}
        assert ProductSite.objects.count() == 2


class TestSite:
    def test_domain_normalized_on_save(self) -> None:
        site = Site.objects.create(domain="HTTPS://WWW.Example.com/")
        assert site.domain == "example.com"

    def test_same_domain_rejected_by_database(self) -> None:
        Site.objects.create(domain="example.com")
        with pytest.raises(IntegrityError), transaction.atomic():
            Site.objects.create(domain="https://www.example.com/")

    def test_deleted_hidden_by_default(self) -> None:
        Site.objects.create(domain="alive.com")
        Site.objects.create(domain="gone.com", is_deleted=True)
        assert list(Site.objects.values_list("domain", flat=True)) == ["alive.com"]
        assert Site.all_objects.count() == 2

    def test_clean_reports_deleted_duplicate(self) -> None:
        Site.objects.create(domain="gone.com", is_deleted=True)
        with pytest.raises(ValidationError, match="помечена удалённой"):
            Site(domain="www.gone.com").full_clean()

    def test_clean_rejects_garbage_domain(self) -> None:
        with pytest.raises(ValidationError) as error:
            Site(domain="https://").full_clean()
        assert "domain" in error.value.message_dict

    def test_cannot_delete_site_with_history(self) -> None:
        site = Site.objects.create(domain="example.com")
        SiteMetric.objects.create(site=site, dr=50)
        with pytest.raises(ProtectedError):
            site.delete()


class TestDatabaseDefaults:
    def test_orm_gets_values_from_database(self) -> None:
        site = Site.objects.create(domain="example.com")
        metric = SiteMetric.objects.create(site=site)
        # Значения пришли из базы (DEFAULT), а не из Python.
        assert site.created_at is not None
        assert metric.checked_at is not None
        assert metric.source == "manual"

    def test_raw_insert_gets_same_defaults(self) -> None:
        # INSERT мимо Django (импорт, psql) получает те же значения.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO sites (domain) VALUES ('raw.com') RETURNING id")
            (site_id,) = cursor.fetchone()
            cursor.execute("INSERT INTO site_prices (site_id) VALUES (%s)", [site_id])
            cursor.execute("SELECT is_deleted, created_at IS NOT NULL FROM sites")
            assert cursor.fetchone() == (False, True)
            cursor.execute("SELECT currency, source FROM site_prices")
            assert cursor.fetchone() == ("EUR", "manual")

    def test_one_transaction_one_timestamp(self) -> None:
        # now(), как в schema.sql: у строк одной транзакции время одинаковое.
        site = Site.objects.create(domain="example.com")
        first = SiteMetric.objects.create(site=site)
        second = SiteMetric.objects.create(site=site)
        assert first.checked_at == second.checked_at


class TestSiteAudit:
    def test_score_out_of_range_rejected(self) -> None:
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        site = Site.objects.create(domain="example.com")
        audit = SiteAudit(
            site=site,
            product=product,
            verdict=AuditVerdict.YES,
            author=AuditAuthor.HUMAN,
            score=101,
        )
        with pytest.raises(ValidationError, match="от 0 до 100"):
            audit.full_clean()
        with pytest.raises(IntegrityError), transaction.atomic():
            audit.save()

    def test_score_may_be_empty(self) -> None:
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        site = Site.objects.create(domain="example.com")
        audit = SiteAudit(
            site=site, product=product, verdict=AuditVerdict.NO, author=AuditAuthor.SYSTEM
        )
        audit.full_clean()
        audit.save()
