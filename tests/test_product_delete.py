"""Удаление продукта (ADR-036, ADR-060): вместе со всем, что без него не живёт."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from apps.content.models import DomainSetting, PromptTemplate
from apps.keywords.models import Keyword
from apps.placements.models import Placement
from apps.sites.models import AuditAuthor, AuditVerdict, Product, ProductSite, Site, SiteAudit

pytestmark = pytest.mark.django_db


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


@pytest.fixture
def product(site: Site) -> Product:
    # Строка продукт × площадка создаётся сама.
    return Product.objects.create(name="Test", domain="test.example")


def _delete_url(product: Product) -> str:
    return reverse("admin:sites_product_delete", args=[product.pk])


def test_unused_product_is_deleted_with_its_rows(
    admin_client: Client, product: Product, site: Site
) -> None:
    DomainSetting.objects.create(key="TOOL_CATEGORIES", product=product, value=["Main"])
    change = admin_client.get(reverse("admin:sites_product_change", args=[product.pk]))
    assert _delete_url(product) in change.content.decode()

    confirm = admin_client.get(_delete_url(product))
    assert confirm.status_code == 200
    assert "Решения по площадкам: <b>1</b>" in confirm.content.decode()

    assert admin_client.post(_delete_url(product), {"post": "yes"}).status_code == 302
    assert not Product.objects.filter(pk=product.pk).exists()
    assert not ProductSite.objects.exists()
    assert not DomainSetting.objects.filter(product__isnull=False).exists()
    assert Site.objects.filter(pk=site.pk).exists()


def _decision(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(status="discarded")


def _imported(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(imported_undecided=True)


def _reason(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(comment="тест")


def _audit(product: Product, site: Site) -> None:
    SiteAudit.objects.create(
        site=site, product=product, verdict=AuditVerdict.NO, author=AuditAuthor.HUMAN
    )


def _placement(product: Product, site: Site) -> None:
    Placement.objects.create(site=site, product=product)


def _keyword(product: Product, site: Site) -> None:
    Keyword.objects.create(product=product, keyword="convert", target_url="https://test.example/")


def _prompt(product: Product, site: Site) -> None:
    PromptTemplate.objects.create(product=product, task="audit", name="аудит")


@pytest.mark.parametrize(
    "history", [_decision, _imported, _reason, _audit, _placement, _keyword, _prompt]
)
def test_product_with_history_is_deleted_with_it(
    admin_client: Client,
    product: Product,
    site: Site,
    history: Callable[[Product, Site], None],
) -> None:
    # С ADR-060 продукт с историей тоже удаляется — вместе с ней; площадки остаются.
    history(product, site)
    assert product.has_history()
    change = admin_client.get(reverse("admin:sites_product_change", args=[product.pk]))
    assert _delete_url(product) in change.content.decode()
    assert admin_client.post(_delete_url(product), {"post": "yes"}).status_code == 302
    assert not Product.objects.filter(pk=product.pk).exists()
    assert not ProductSite.objects.exists()
    assert not Placement.objects.exists()
    assert not Keyword.objects.exists()
    assert Site.objects.filter(pk=site.pk).exists()


def test_bulk_delete_offered(admin_client: Client, product: Product) -> None:
    changelist = admin_client.get(reverse("admin:sites_product_changelist"))
    assert "delete_selected" in changelist.content.decode()
