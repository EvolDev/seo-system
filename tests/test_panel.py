"""Панель записи на сервере (E9-11, ADR-048): форма для панели, запись — JSON.

Запрос с заголовком X-Seo-Partial к админке с `panel = True` получает ту же
страницу формы, но с шапкой и низом панели; записали — JSON с подписью, а не
переход на список; ошибка в форме — снова форма, с подсказками. Браузерную
часть проверяет tests/e2e/test_panel.py.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib import messages
from django.http import HttpRequest
from django.test import Client
from django.urls import reverse

from apps.content.models import DomainSetting
from apps.keywords.models import Keyword
from apps.placements.models import Placement
from apps.sites.admin import SellerAdmin
from apps.sites.models import Product, Seller, Site, SiteList, SiteMetric

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}


def _product() -> Product:
    return Product.objects.get_or_create(name="Convertio", domain="convertio.co")[0]


def _site() -> Site:
    return Site.objects.get_or_create(domain="example.com")[0]


# Записи списков с панелью: имя адреса в админке и как завести запись.
RECORDS: dict[str, Callable[[], Any]] = {
    "placements_placement": lambda: Placement.objects.create(site=_site(), product=_product()),
    "sites_site": _site,
    "sites_seller": lambda: Seller.objects.create(name="Adsy"),
    "sites_sitelist": lambda: SiteList.objects.create(name="Октябрь"),
    "sites_product": _product,
    "keywords_keyword": lambda: Keyword.objects.create(
        product=_product(), keyword="mp4 to mp3", target_url="https://convertio.co/"
    ),
    "content_domainsetting": lambda: DomainSetting.objects.create(
        key="LINK_POSITION_FIRST", value=250
    ),
}


def _change_url(name: str, obj: Any) -> str:
    return reverse(f"admin:{name}_change", args=[obj.pk])


def _seller_form(**fields: str) -> dict[str, str]:
    return {"name": "Adsy", "currency": "EUR", "contacts": "", "notes": "", **fields}


@pytest.mark.parametrize("name", RECORDS)
def test_record_form_for_panel(admin_client: Client, name: str) -> None:
    obj = RECORDS[name]()
    url = _change_url(name, obj)
    page = admin_client.get(url, headers=PARTIAL).content.decode()
    assert 'class="seo-panel-head"' in page
    assert "data-panel-save" in page
    assert "data-panel-cancel" in page
    assert f'href="{url}"' in page  # «Открыть полностью» — та же форма страницей
    # «История» и удаление — только на полной странице.
    assert "historylink" not in page
    assert 'class="deletelink"' not in page

    full = admin_client.get(url).content.decode()
    assert "seo-panel-head" not in full
    assert "historylink" in full


@pytest.mark.parametrize("name", RECORDS)
def test_list_marked_for_panel(admin_client: Client, name: str) -> None:
    page = admin_client.get(reverse(f"admin:{name}_changelist")).content.decode()
    assert "seo-panel-list" in page


def test_other_lists_and_forms_without_panel(admin_client: Client) -> None:
    """Снимки — не в панели: новый замер там — «Сохранить как новый объект»."""
    metric = SiteMetric.objects.create(site=_site(), dr=40)
    assert (
        "seo-panel-list"
        not in admin_client.get(reverse("admin:sites_sitemetric_changelist")).content.decode()
    )
    page = admin_client.get(
        reverse("admin:sites_sitemetric_change", args=[metric.pk]), headers=PARTIAL
    ).content.decode()
    assert "seo-panel-head" not in page


def test_title_is_the_record(admin_client: Client) -> None:
    placement = RECORDS["placements_placement"]()
    page = admin_client.get(
        _change_url("placements_placement", placement), headers=PARTIAL
    ).content.decode()
    assert '<h2 class="seo-panel-title">example.com · Convertio</h2>' in page


class TestSave:
    def test_saved_answer_is_json(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="Adsy")
        response = admin_client.post(
            _change_url("sites_seller", seller), _seller_form(contacts="@adsy"), headers=PARTIAL
        )
        assert response.status_code == 200
        assert response.json() == {
            "saved": True,
            "pk": seller.pk,
            "message": "Продавец «Adsy» — сохранено.",
            "notes": [],
        }
        seller.refresh_from_db()
        assert seller.contacts == "@adsy"

    def test_no_message_left_for_next_screen(self, admin_client: Client) -> None:
        """Подпись ушла в панель — на следующем экране «успешно изменено» не всплывает."""
        seller = Seller.objects.create(name="Adsy")
        admin_client.post(_change_url("sites_seller", seller), _seller_form(), headers=PARTIAL)
        page = admin_client.get(reverse("admin:sites_seller_changelist")).content.decode()
        assert "messagelist" not in page

    def test_messages_of_saving_go_to_panel(
        self, admin_client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def save_model(self: SellerAdmin, request: HttpRequest, *args: Any) -> None:
            self.message_user(request, "Цены продавца пересчитаны.", messages.WARNING)

        monkeypatch.setattr(SellerAdmin, "save_model", save_model)
        seller = Seller.objects.create(name="Adsy")
        response = admin_client.post(
            _change_url("sites_seller", seller), _seller_form(), headers=PARTIAL
        )
        assert response.json()["notes"] == ["Цены продавца пересчитаны."]
        page = admin_client.get(reverse("admin:sites_seller_changelist")).content.decode()
        assert "Цены продавца пересчитаны" not in page

    def test_form_error_stays_in_panel(self, admin_client: Client) -> None:
        seller = Seller.objects.create(name="Adsy")
        response = admin_client.post(
            _change_url("sites_seller", seller), _seller_form(name=""), headers=PARTIAL
        )
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/html")
        page = response.content.decode()
        assert "errorlist" in page
        assert 'class="seo-panel-head"' in page
        seller.refresh_from_db()
        assert seller.name == "Adsy"

    def test_add_in_panel(self, admin_client: Client) -> None:
        url = reverse("admin:sites_seller_add")
        page = admin_client.get(url, headers=PARTIAL).content.decode()
        assert '<h2 class="seo-panel-title">Продавец — новая запись</h2>' in page
        response = admin_client.post(url, _seller_form(name="Collab"), headers=PARTIAL)
        seller = Seller.objects.get(name="Collab")
        assert json.loads(response.content) == {
            "saved": True,
            "pk": seller.pk,
            "message": "Продавец «Collab» — добавлено.",
            "notes": [],
        }

    def test_full_page_saves_as_before(self, admin_client: Client) -> None:
        """Без заголовка — штатно: переход на список и сообщение там."""
        seller = Seller.objects.create(name="Adsy")
        response = admin_client.post(_change_url("sites_seller", seller), _seller_form())
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("admin:sites_seller_changelist"))

    def test_view_only_has_close_only(self, client: Client, django_user_model: Any) -> None:
        """Только просмотр: в низу панели — «Закрыть», без «Сохранить»."""
        from django.contrib.auth.models import Permission

        user = django_user_model.objects.create_user("viewer", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_seller"))
        client.force_login(user)
        seller = Seller.objects.create(name="Adsy")
        page = client.get(_change_url("sites_seller", seller), headers=PARTIAL).content.decode()
        assert "data-panel-save" not in page
        assert ">Закрыть</button>" in page


class TestRowOutsideFilters:
    """Запись перестала подходить под фильтры — список отдаёт её строку одну (_seo_row)."""

    def test_placement_row_ignores_filters(self, admin_client: Client) -> None:
        placement = Placement.objects.create(site=_site(), product=_product())
        Placement.objects.create(site=Site.objects.create(domain="other.com"), product=_product())
        Placement.objects.filter(pk=placement.pk).update(status="placed")
        url = reverse("admin:placements_placement_changelist")
        response = admin_client.get(
            url, {"status__exact": "in_work", "_seo_row": str(placement.pk)}
        )
        assert response.status_code == 200
        results = response.context["cl"].result_list
        assert [row.pk for row in results] == [placement.pk]
        assert "Опубликовано" in response.content.decode()
        # Фильтры остались прежними: параметр строки не считается условием отбора.
        assert response.context["cl"].get_filters_params() == {"status__exact": ["in_work"]}
        # Под фильтр «Запланировано» запись больше не подходит — строка блёклая.
        assert response["X-Seo-Row-Match"] == "0"
        matching = admin_client.get(url, {"status__exact": "placed", "_seo_row": str(placement.pk)})
        assert matching["X-Seo-Row-Match"] == "1"

    def test_row_does_not_count_whole_list(
        self, admin_client: Client, django_assert_max_num_queries: Any
    ) -> None:
        placement = Placement.objects.create(site=_site(), product=_product())
        url = reverse("admin:placements_placement_changelist")
        # Запросы — как у обычной страницы списка (сессия, фильтры, «Мои фильтры»),
        # а «всего N» — по одной строке.
        with django_assert_max_num_queries(20):
            response = admin_client.get(url, {"_seo_row": str(placement.pk)})
        assert response.context["cl"].full_result_count == 1

    def test_without_param_list_as_usual(self, admin_client: Client) -> None:
        placement = Placement.objects.create(site=_site(), product=_product())
        url = reverse("admin:placements_placement_changelist")
        response = admin_client.get(url, {"status__exact": "placed"})
        assert placement not in response.context["cl"].result_list

    def test_bad_row_is_empty(self, admin_client: Client) -> None:
        url = reverse("admin:placements_placement_changelist")
        response = admin_client.get(url, {"_seo_row": "x"})
        assert response.status_code == 200
        assert list(response.context["cl"].result_list) == []
