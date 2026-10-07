"""«Домены для Ahrefs»: домены рабочего списка файлами по 500 (E1-10, ADR-045)."""

import io
import zipfile

import pytest
from django.test import Client
from django.urls import reverse

from apps.sites import ahrefs_domains, latest
from apps.sites.models import Site, SiteList, SiteListItem, SiteMetric

pytestmark = pytest.mark.django_db


@pytest.fixture
def big_list() -> SiteList:
    """1201 площадка: DR у каждой своя, у одной нет, одна удалена."""
    site_list = SiteList.objects.create(name="Collaborator · 02.10.2026")
    sites = Site.objects.bulk_create(Site(domain=f"site{n:04}.com") for n in range(1202))
    SiteMetric.objects.bulk_create(
        SiteMetric(site=site, dr=n % 100) for n, site in enumerate(sites) if n != 7
    )
    SiteListItem.objects.bulk_create(SiteListItem(site_list=site_list, site=s) for s in sites)
    Site.objects.filter(domain="site0099.com").update(is_deleted=True)
    # Площадки и замеры заведены пачками, минуя save(): копию «площадки на
    # сегодня» пересчитываем явно, как это делает загрузка (E1-11).
    latest.refresh_all()
    return site_list


def test_parts_by_500_ordered_by_dr(big_list: SiteList) -> None:
    parts = ahrefs_domains.parts(big_list)
    assert [len(p.domains) for p in parts] == [500, 500, 201]
    assert [(p.number, p.total, p.start, p.end) for p in parts] == [
        (1, 3, 1, 500),
        (2, 3, 501, 1000),
        (3, 3, 1001, 1201),
    ]
    domains = [d for p in parts for d in p.domains]
    assert len(set(domains)) == 1201
    assert "site0099.com" not in domains  # удалённая
    assert domains[0] == "site0199.com"  # DR 99, при равном — по алфавиту
    assert parts[0].top_dr == 99
    assert domains[-1] == "site0007.com"  # без DR — в конце
    assert parts[0].text().splitlines()[:2] == ["site0199.com", "site0299.com"]
    assert parts[0].file_name(big_list) == "ahrefs-collaborator-02102026-1-of-3.txt"


def test_screen_part_and_archive(admin_client: Client, big_list: SiteList) -> None:
    url = reverse("admin:sites_sitelist_ahrefs", args=[big_list.pk])
    page = admin_client.get(url).content.decode()
    assert "1201 доменов, 3 файла по 500" in page
    assert "Скачать все части архивом" in page

    response = admin_client.get(url, {"part": "3"})
    assert response["Content-Type"] == "text/plain"
    assert "ahrefs-collaborator-02102026-3-of-3.txt" in response["Content-Disposition"]
    assert len(response.content.decode().splitlines()) == 201

    response = admin_client.get(url, {"zip": "1"})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names == [f"ahrefs-collaborator-02102026-{n}-of-3.txt" for n in (1, 2, 3)]
        assert len(archive.read(names[0]).decode().splitlines()) == 500


def test_unknown_part_goes_back(admin_client: Client, big_list: SiteList) -> None:
    url = reverse("admin:sites_sitelist_ahrefs", args=[big_list.pk])
    assert admin_client.get(url, {"part": "9"})["Location"] == url


def test_cyrillic_list_name(admin_client: Client) -> None:
    site_list = SiteList.objects.create(name="Таблица линкбилдинга · 27.09.2026")
    SiteListItem.objects.create(site_list=site_list, site=Site.objects.create(domain="a.com"))
    url = reverse("admin:sites_sitelist_ahrefs", args=[site_list.pk])
    response = admin_client.get(url, {"part": "1"})
    assert "filename*=utf-8''ahrefs-" in response["Content-Disposition"]
    assert response.content.decode() == "a.com\n"


def test_link_in_lists_screen(admin_client: Client, big_list: SiteList) -> None:
    page = admin_client.get(reverse("admin:sites_sitelist_changelist")).content.decode()
    assert reverse("admin:sites_sitelist_ahrefs", args=[big_list.pk]) in page
