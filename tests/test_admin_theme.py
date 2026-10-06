"""Тема админки Admin Interface и расцветки поверх неё (E9-08, ADR-038).

Расцветка — атрибут data-palette у <html>: ставит скрипт в <head> шаблона
config/templates/admin/base_site.html, цвета — config/static/seo/admin-palettes.css.
Список расцветок живёт в трёх местах (кнопки переключателя, скрипт, стили),
поэтому тест сверяет, что он везде одинаковый.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.admin.views.main import ChangeList
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse

from apps.sites.admin import ProductFrameFilter, SiteListFilter
from apps.sites.models import Product, SiteList

pytestmark = pytest.mark.django_db

PALETTES = {"apple-light": "light", "google-dark": "dark", "emerald": "light", "ahrefs": "dark"}
TEMPLATE = Path(settings.BASE_DIR) / "config" / "templates" / "admin" / "base_site.html"


def _stylesheet() -> str:
    path = finders.find("seo/admin-palettes.css")
    assert isinstance(path, str), "стили расцветок не найдены в статике"
    return Path(path).read_text(encoding="utf-8")


def _index(client: Client) -> str:
    response = client.get(reverse("admin:index"))
    assert response.status_code == 200
    return response.content.decode()


def test_pages_use_admin_interface_with_palettes(admin_client: Client) -> None:
    content = _index(admin_client)
    assert "admin-interface" in content  # класс <body> от темы
    assert "seo/admin-palettes.css" in content
    assert "<span>SEO-система</span>" in content
    assert "SEO-система</title>" in content


def test_palette_is_set_before_first_paint(admin_client: Client) -> None:
    # Скрипт в <head>: иначе страница сначала отрисуется в чужих цветах.
    head = _index(admin_client).split("</head>", 1)[0]
    assert 'localStorage.getItem("admin-palette")' in head
    assert "document.documentElement.dataset.palette = palette" in head


def test_switcher_offers_every_palette(admin_client: Client) -> None:
    content = _index(admin_client)
    assert 'id="palette-switch"' in content
    for key, mode in PALETTES.items():
        assert f'data-palette="{key}" data-mode="{mode}"' in content


def test_palette_lists_agree() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    buttons = dict(re.findall(r'data-palette="([a-z-]+)" data-mode="(light|dark)"', template))
    script = re.search(r"const modes = \{(.*?)\};", template)
    assert script is not None
    in_script = dict(re.findall(r'"?([a-z-]+)"?: "(light|dark)"', script.group(1)))
    block = r'html\[data-palette="([a-z-]+)"\] body\.admin-interface \{\s*'
    in_css = dict(re.findall(block + r"color-scheme: (light|dark);", _stylesheet()))
    assert buttons == in_script == in_css == PALETTES


def test_page_transitions_do_not_blank_the_screen() -> None:
    assert "@view-transition { navigation: auto; }" in _stylesheet()


def test_debug_toolbar_starts_collapsed() -> None:
    # Развёрнутая панель закрывала шапку и переключатель расцветки.
    assert settings.DEBUG_TOOLBAR_CONFIG["SHOW_COLLAPSED"] is True


def test_product_and_list_filters_open_on_all(admin_client: Client) -> None:
    # Оба фильтра «Площадок» открываются на «Все» — своим пунктом, а не штатным (E1-19).
    Product.objects.create(name="Convertio", domain="convertio.co")
    SiteList.objects.create(name="Сентябрь")
    response = admin_client.get(reverse("admin:sites_productsitelatest_changelist"))
    changelist: ChangeList = response.context["cl"]
    chosen = {
        type(spec).__name__: [
            (choice["display"], choice["selected"]) for choice in spec.choices(changelist)
        ]
        for spec in changelist.filter_specs
        if isinstance(spec, ProductFrameFilter | SiteListFilter)
    }
    assert chosen == {
        "ProductFrameFilter": [("Все", True), ("Convertio", False)],
        "SiteListFilter": [("Сентябрь", False), ("Все площадки", True)],
    }
