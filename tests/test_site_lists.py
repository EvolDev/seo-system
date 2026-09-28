"""Рабочие списки площадок: модели и админка (E1-04, ADR-033)."""

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.sites.models import Site, SiteList, SiteListItem

pytestmark = pytest.mark.django_db


@pytest.fixture
def september() -> SiteList:
    return SiteList.objects.create(name="Сентябрь 2026", source="test.xlsx")


class TestModels:
    def test_site_is_once_per_list(self, september: SiteList) -> None:
        site = Site.objects.create(domain="example.com")
        SiteListItem.objects.create(site_list=september, site=site)
        with pytest.raises(IntegrityError), transaction.atomic():
            SiteListItem.objects.create(site_list=september, site=site)

    def test_same_site_in_several_lists(self, september: SiteList) -> None:
        october = SiteList.objects.create(name="Октябрь 2026")
        site = Site.objects.create(domain="example.com")
        SiteListItem.objects.create(site_list=september, site=site, first_seen=True)
        SiteListItem.objects.create(site_list=october, site=site)
        assert set(site.list_items.values_list("site_list__name", "first_seen")) == {
            ("Сентябрь 2026", True),
            ("Октябрь 2026", False),
        }

    def test_list_name_is_unique(self, september: SiteList) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SiteList.objects.create(name="Сентябрь 2026")

    def test_first_seen_defaults_to_false(self, september: SiteList) -> None:
        site = Site.objects.create(domain="example.com")
        item = SiteListItem.objects.create(site_list=september, site=site)
        item.refresh_from_db()
        assert item.first_seen is False
        assert item.added_at is not None


class TestAdmin:
    def test_list_shows_counts_and_link_to_sites(
        self, admin_client: Client, september: SiteList
    ) -> None:
        for domain, first_seen in [("a.com", True), ("b.com", True), ("c.com", False)]:
            site = Site.objects.create(domain=domain)
            SiteListItem.objects.create(site_list=september, site=site, first_seen=first_seen)
        response = admin_client.get(reverse("admin:sites_sitelist_changelist"))
        assert response.status_code == 200
        row = response.context["cl"].result_list[0]
        assert (row.sites_total, row.first_seen_total) == (3, 2)
        # Число ведёт на рабочий экран «Площадки» с этим списком (E9-08).
        sites_url = reverse("admin:sites_productsitelatest_changelist")
        assert f"{sites_url}?list={september.pk}" in response.content.decode()

    def test_items_filtered_by_list(self, admin_client: Client, september: SiteList) -> None:
        october = SiteList.objects.create(name="Октябрь 2026")
        SiteListItem.objects.create(site_list=september, site=Site.objects.create(domain="a.com"))
        SiteListItem.objects.create(site_list=october, site=Site.objects.create(domain="b.com"))
        response = admin_client.get(
            reverse("admin:sites_sitelistitem_changelist"),
            {"site_list__id__exact": september.pk},
        )
        assert [item.site.domain for item in response.context["cl"].result_list] == ["a.com"]

    def test_items_are_read_only(self, admin_client: Client, september: SiteList) -> None:
        site = Site.objects.create(domain="example.com")
        item = SiteListItem.objects.create(site_list=september, site=site)
        assert admin_client.get(reverse("admin:sites_sitelistitem_add")).status_code == 403
        response = admin_client.get(reverse("admin:sites_sitelistitem_change", args=[item.pk]))
        assert response.status_code == 200
        assert response.context["has_change_permission"] is False

    @pytest.mark.parametrize("model", ["sitelist", "sitelistitem"])
    def test_delete_disabled(self, admin_client: Client, model: str) -> None:
        response = admin_client.get(reverse(f"admin:sites_{model}_changelist"))
        assert response.status_code == 200
        model_admin = response.context["cl"].model_admin
        assert model_admin.has_delete_permission(response.wsgi_request) is False
