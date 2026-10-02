"""Админка без перезагрузки страниц (E9-09, ADR-046): что видно без браузера.

Само поведение — переходы, формы, фильтры, «Назад» — проверяют браузерные
тесты tests/e2e (make e2e). Здесь — что скрипт подключён везде и что шаблоны
фильтров больше не уводят страницу сами через window.location.
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.sites.models import Product, Site, SiteList, SiteListItem

pytestmark = pytest.mark.django_db

TEMPLATES = Path(settings.BASE_DIR) / "config" / "templates"


@pytest.mark.parametrize(
    "url",
    [
        reverse("admin:index"),
        reverse("admin:sites_productsitelatest_changelist"),
        reverse("admin:placements_placement_add"),
        reverse("admin:sites_upload_add"),
        reverse("admin:password_change"),
    ],
)
def test_every_admin_page_loads_soft_navigation(admin_client: Client, url: str) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    page = admin_client.get(url).content.decode()
    # С версией в адресе (config/assets.py): после правки браузер возьмёт новый файл.
    assert "/static/seo/soft-nav.js?v=" in page
    assert "/static/seo/soft-nav.css?v=" in page


def test_filters_on_sites_screen_do_not_navigate_themselves(admin_client: Client) -> None:
    """Выпадающие и «от — до» — без своих скриптов с window.location."""
    Product.objects.create(name="Convertio", domain="convertio.co")
    site_list = SiteList.objects.create(name="Октябрь")
    SiteListItem.objects.create(site_list=site_list, site=Site.objects.create(domain="a.com"))
    page = admin_client.get(reverse("admin:sites_productsitelatest_changelist")).content.decode()
    assert 'class="list-filter-dropdown"' in page
    assert 'class="numericrangefilter"' in page
    assert "dropdown-filter.js" not in page
    assert "window.location" not in page


@pytest.mark.parametrize(
    "template", ["admin_interface/dropdown_filter.html", "rangefilter/numeric_filter.html"]
)
def test_filter_templates_have_no_navigation_script(template: str) -> None:
    text = (TEMPLATES / template).read_text()
    assert "<script" not in text


def test_debug_toolbar_follows_soft_navigation() -> None:
    # Панель SQL показывает запросы подгруженного экрана, а не только первого.
    assert settings.DEBUG_TOOLBAR_CONFIG["UPDATE_ON_FETCH"] is True
