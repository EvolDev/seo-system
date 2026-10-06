"""Меню админки по работе человека, главная и заголовки (E9-08).

Критерий приёмки: в меню нет двух пунктов с похожими названиями, служебные
таблицы не на первом уровне. Меню собирает config/admin_site.py.
"""

from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.sites.models import Product, ProductSite, Site, SiteList, SiteStatus
from config.admin_site import HIDDEN, SeoAdminSite
from config.forms import ChoiceButtons

pytestmark = pytest.mark.django_db

SERVICE_TABLES = {
    "Решения по площадкам",
    "Метрики",
    "Предложения продавцов",
    "Курсы валют",
    "Проверки серости",
    "Аудиты",
    "Позиции ключей",
    "Запуски задач",
    "Расход API",
}


def _menu(client: Client) -> list[dict[str, Any]]:
    response = client.get(reverse("admin:index"))
    assert response.status_code == 200
    menu: list[dict[str, Any]] = response.context["available_apps"]
    return menu


def _names(group: dict[str, Any]) -> list[str]:
    return [str(model["name"]) for model in group["models"]]


def test_admin_site_is_ours() -> None:
    assert isinstance(admin.site, SeoAdminSite)


def test_groups_follow_the_work(admin_client: Client) -> None:
    menu = _menu(admin_client)
    assert [group["name"] for group in menu] == ["Работа", "Справочники", "Настройки", "Служебное"]
    assert _names(menu[0]) == ["Площадки", "Загрузки", "Размещения", "Счета"]
    # Анкоры продукта — справочник (E3-05, ADR-059); форма ключа — в «Служебном».
    assert _names(menu[1]) == [
        "Каталог площадок",
        "Продавцы",
        "Рабочие списки",
        "Продукты",
        "Анкоры",
        "Ссылающиеся домены",
    ]
    assert "Ключи" in _names(menu[3])


def test_no_similar_names(admin_client: Client) -> None:
    names = [name.lower() for group in _menu(admin_client) for name in _names(group)]
    assert len(names) == len(set(names))
    # Раньше было четыре пункта «Площадки…»; теперь «Площадки» — один рабочий экран.
    assert [name for name in names if name.startswith("площадк")] == ["площадки"]


def test_service_tables_only_in_service_group(admin_client: Client) -> None:
    groups = {group["app_label"]: set(_names(group)) for group in _menu(admin_client)}
    for code in ("work", "reference", "settings"):
        assert not SERVICE_TABLES & groups[code]
    assert groups["service"] >= SERVICE_TABLES


def test_every_registered_model_once(admin_client: Client) -> None:
    in_menu = [
        (model["model"]._meta.app_label, model["model"]._meta.model_name)
        for group in _menu(admin_client)
        for model in group["models"]
    ]
    registered = {(model._meta.app_label, model._meta.model_name) for model in admin.site._registry}
    assert len(in_menu) == len(set(in_menu))
    assert set(in_menu) == registered - HIDDEN


def test_hidden_tables_still_open_by_link(admin_client: Client) -> None:
    # Строки рабочих списков — не в меню, но страница работает (по ссылке из списка).
    response = admin_client.get(reverse("admin:sites_sitelistitem_changelist"))
    assert response.status_code == 200


def test_service_group_collapsed_by_default(admin_client: Client) -> None:
    head = admin_client.get(reverse("admin:index")).content.decode().split("</head>", 1)[0]
    assert "admin-interface.foldable-apps_app-service_collapsed" in head


def test_home_starts_with_work(admin_client: Client) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    SiteList.objects.create(name="Сентябрь 2026")
    response = admin_client.get(reverse("admin:index"))
    cards = response.context["home_cards"]
    assert [card["name"] for card in cards] == [
        "Площадки",
        "Загрузки",
        "Размещения",
        "Счета",
    ]
    assert cards[0]["url"] == reverse("admin:sites_productsitelatest_changelist")
    assert "Convertio, список «Сентябрь 2026»" in cards[0]["note"]
    # «Работа» — карточками; ниже — остальные группы, без повтора.
    below = [group["app_label"] for group in response.context["home_app_list"]]
    assert below == ["reference", "settings", "service"]


def test_home_without_products(admin_client: Client) -> None:
    cards = admin_client.get(reverse("admin:index")).context["home_cards"]
    # Загрузки от продукта не зависят: «0 предложений ждут разбора» есть и без него.
    assert [(card["value"], card["note"]) for card in cards] == [
        ("", ""),
        ("0", "предложений ждут разбора"),
        ("", ""),
        ("0", "к оплате"),
    ]


def test_no_view_site_link(admin_client: Client) -> None:
    # Публичного сайта нет — ссылка «Открыть сайт» вела бы в пустоту.
    assert "Открыть сайт" not in admin_client.get(reverse("admin:index")).content.decode()


def test_titles_read_naturally(admin_client: Client) -> None:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    changelist = admin_client.get(reverse("admin:sites_productsitelatest_changelist"))
    assert changelist.context["title"] == "Площадки"
    assert "Выберите" not in changelist.content.decode()
    change = admin_client.get(reverse("admin:sites_product_change", args=[product.pk]))
    assert (change.context["title"], change.context["subtitle"]) == ("Продукт", "Convertio")
    add = admin_client.get(reverse("admin:sites_product_add"))
    assert add.context["title"] == "Продукт — новая запись"


def test_filter_titles_without_by(admin_client: Client) -> None:
    Product.objects.create(name="Convertio", domain="convertio.co")
    content = admin_client.get(reverse("admin:sites_productsitelatest_changelist")).content.decode()
    assert "По продукт" not in content
    assert "По DR" not in content
    assert ">Продукт</h3>" in content
    assert "<summary>DR</summary>" in content


def test_breadcrumbs_without_django_app(admin_client: Client) -> None:
    # «Начало › Площадки › Площадки»: средняя крошка — приложение Django, её нет.
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    app_url = reverse("admin:app_list", kwargs={"app_label": "sites"})
    for url in (
        reverse("admin:sites_productsitelatest_changelist"),
        reverse("admin:sites_product_change", args=[product.pk]),
    ):
        content = admin_client.get(url).content.decode()
        breadcrumbs = content.split('<div class="breadcrumbs">', 1)[1].split("</div>", 1)[0]
        assert f'href="{app_url}"' not in breadcrumbs


def test_short_text_fields_are_single_line(admin_client: Client) -> None:
    form = admin_client.get(reverse("admin:sites_product_add")).context["adminform"].form
    assert form.fields["name"].widget.input_type == "text"
    assert form.fields["domain"].widget.input_type == "text"


def test_status_fields_stay_dropdowns(admin_client: Client) -> None:
    """Статусы — перечисления Postgres (в Django это TextField) — выпадающим списком.

    С E9-08 до E9-09 они стали полем ввода: статус приходилось набирать
    кодом, и форма отвечала «New нет среди допустимых значений».
    """
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="coingabbar.com")
    row = ProductSite.objects.get(site=site, product=product)
    response = admin_client.get(reverse("admin:sites_productsite_change", args=[row.pk]))
    status = response.context["adminform"].form.fields["status"]
    # С E9-11 — кнопками по порядку (config/forms.py); это тоже выбор из списка.
    assert isinstance(status.widget, ChoiceButtons)
    assert [value for value, _ in status.choices] == [value for value, _ in SiteStatus.choices]

    response = admin_client.post(
        reverse("admin:sites_productsite_change", args=[row.pk]),
        {"status": SiteStatus.IN_WORK, "comment": "", "content_profile": "null"},
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.status == SiteStatus.IN_WORK

    placement = admin_client.get(reverse("admin:placements_placement_add"))
    assert isinstance(placement.context["adminform"].form.fields["status"].widget, ChoiceButtons)
