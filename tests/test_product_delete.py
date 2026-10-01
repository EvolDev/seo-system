"""Продукт, заведённый по ошибке, удаляется, пока с ним не работали (ADR-036)."""

from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError
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
    assert "пустые строки" in confirm.content.decode()

    assert admin_client.post(_delete_url(product), {"post": "yes"}).status_code == 302
    assert not Product.objects.filter(pk=product.pk).exists()
    assert not ProductSite.objects.exists()
    assert not DomainSetting.objects.filter(product__isnull=False).exists()
    assert Site.objects.filter(pk=site.pk).exists()


def _decision(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(status="rejected")


def _imported(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(imported_undecided=True)


def _reason(product: Product, site: Site) -> None:
    ProductSite.objects.filter(product=product).update(reject_reason="тест")


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
def test_product_with_history_is_not_deleted(
    admin_client: Client,
    product: Product,
    site: Site,
    history: Callable[[Product, Site], None],
) -> None:
    history(product, site)
    assert product.has_history()
    change = admin_client.get(reverse("admin:sites_product_change", args=[product.pk]))
    assert _delete_url(product) not in change.content.decode()
    assert admin_client.post(_delete_url(product), {"post": "yes"}).status_code == 403
    with pytest.raises(ValidationError):
        product.delete_unused()
    assert Product.objects.filter(pk=product.pk).exists()


def test_no_bulk_delete(admin_client: Client, product: Product) -> None:
    changelist = admin_client.get(reverse("admin:sites_product_changelist"))
    assert "delete_selected" not in changelist.content.decode()
