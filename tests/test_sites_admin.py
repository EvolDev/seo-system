"""Админка блока 1: снапшоты, запрет удаления, поиск, удалённые (E1-01)."""

import datetime as dt

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.sites.models import Product, ProductSite, Site, SiteMetric

pytestmark = pytest.mark.django_db


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="example.com")


def _metric(site: Site, dr: int, days_ago: int) -> SiteMetric:
    return SiteMetric.objects.create(
        site=site, dr=dr, checked_at=timezone.now() - dt.timedelta(days=days_ago)
    )


def _change_url(obj: SiteMetric) -> str:
    return reverse("admin:sites_sitemetric_change", args=[obj.pk])


def _metric_form(site: Site, dr: int) -> dict[str, str]:
    return {"site": str(site.pk), "dr": str(dr), "source": "manual"}


class TestSnapshots:
    def test_latest_is_editable(self, admin_client: Client, site: Site) -> None:
        latest = _metric(site, dr=40, days_ago=0)
        response = admin_client.post(_change_url(latest), _metric_form(site, 41))
        assert response.status_code == 302
        latest.refresh_from_db()
        assert latest.dr == 41

    def test_older_is_read_only(self, admin_client: Client, site: Site) -> None:
        older = _metric(site, dr=30, days_ago=30)
        _metric(site, dr=40, days_ago=0)
        assert admin_client.get(_change_url(older)).status_code == 200
        assert admin_client.post(_change_url(older), _metric_form(site, 99)).status_code == 403
        older.refresh_from_db()
        assert older.dr == 30

    def test_save_as_new_keeps_history(self, admin_client: Client, site: Site) -> None:
        latest = _metric(site, dr=40, days_ago=30)
        data = {**_metric_form(site, 45), "_saveasnew": "1"}
        assert admin_client.post(_change_url(latest), data).status_code == 302
        old, new = SiteMetric.objects.order_by("checked_at")
        assert (old.pk, old.dr) == (latest.pk, 40)
        # Дата замера у нового снапшота своя, а не скопированная из формы.
        assert new.dr == 45
        assert new.checked_at > old.checked_at

    def test_audit_latest_is_per_product(self, admin_client: Client, site: Site) -> None:
        convertio = Product.objects.create(name="Convertio", domain="convertio.co")
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        audits = [
            site.audits.create(product=product, verdict="yes", author="human")
            for product in (convertio, clideo)
        ]
        # Аудит под Clideo не делает аудит под Convertio устаревшим.
        for audit in audits:
            url = reverse("admin:sites_siteaudit_change", args=[audit.pk])
            response = admin_client.get(url)
            assert response.context["has_change_permission"] is True


@pytest.mark.parametrize(
    "model", ["product", "site", "productsite", "sitemetric", "siteprice", "grayscan", "siteaudit"]
)
def test_delete_disabled(admin_client: Client, model: str) -> None:
    response = admin_client.get(reverse(f"admin:sites_{model}_changelist"))
    assert response.status_code == 200
    assert response.context["cl"].model_admin.has_delete_permission(response.wsgi_request) is False


def test_product_site_rows_are_not_added_by_hand(admin_client: Client) -> None:
    assert admin_client.get(reverse("admin:sites_productsite_add")).status_code == 403


def test_search_by_pasted_url(admin_client: Client, site: Site) -> None:
    Site.objects.create(domain="other.com")
    url = reverse("admin:sites_site_changelist")
    response = admin_client.get(url, {"q": "https://www.Example.com/some-article"})
    assert [s.domain for s in response.context["cl"].result_list] == ["example.com"]


def test_deleted_hidden_until_asked(admin_client: Client, site: Site) -> None:
    Site.objects.create(domain="gone.com", is_deleted=True)
    url = reverse("admin:sites_site_changelist")

    def domains(**params: str) -> list[str]:
        response = admin_client.get(url, params)
        return sorted(s.domain for s in response.context["cl"].result_list)

    assert domains() == ["example.com"]
    assert domains(deleted="yes") == ["gone.com"]
    assert domains(deleted="all") == ["example.com", "gone.com"]


def test_site_page_shows_status_per_product(admin_client: Client, site: Site) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    response = admin_client.get(reverse("admin:sites_site_change", args=[site.pk]))
    assert response.status_code == 200
    assert ProductSite.objects.filter(site=site).count() == 1
    assert "Convertio" in response.content.decode()


def test_add_product_in_admin(admin_client: Client, site: Site) -> None:
    url = reverse("admin:sites_product_add")
    data = {"name": "Convertio", "domain": "https://Convertio.co/", "is_active": "on"}
    assert admin_client.post(url, data).status_code == 302
    product = Product.objects.get()
    assert product.domain == "convertio.co"
    assert list(ProductSite.objects.values_list("product_id", "site_id")) == [(product.pk, site.pk)]


def test_add_site_in_admin(admin_client: Client) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    url = reverse("admin:sites_site_add")
    data = {
        "domain": "HTTPS://WWW.New-Site.com/blog/",
        # Служебные поля встроенной таблицы статусов по продуктам.
        "product_sites-TOTAL_FORMS": "0",
        "product_sites-INITIAL_FORMS": "0",
    }
    assert admin_client.post(url, data).status_code == 302
    site = Site.objects.get()
    assert site.domain == "new-site.com"
    assert ProductSite.objects.filter(site=site).count() == 1


def test_add_site_with_existing_domain_shows_error(admin_client: Client, site: Site) -> None:
    url = reverse("admin:sites_site_add")
    data = {
        "domain": "www.example.com",
        "product_sites-TOTAL_FORMS": "0",
        "product_sites-INITIAL_FORMS": "0",
    }
    response = admin_client.post(url, data)
    assert response.status_code == 200
    assert response.context["adminform"].form.errors["domain"]
