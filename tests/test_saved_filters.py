"""«Мои фильтры» на сервере (E9-10, ADR-050): наборы, блок над колонкой фильтров.

Набор свой у каждого пользователя, удаляется пометкой и возвращается
«Отменить». Браузерную часть — выбор, «Как в прошлый раз» — проверяет
tests/e2e/test_saved_filters.py.
"""

import json
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.workspace.models import SavedFilter
from apps.workspace.saved_filters import clean_query, same_query

pytestmark = pytest.mark.django_db

SCREEN = "sites.productsitelatest"
LIST_URL = "admin:sites_productsitelatest_changelist"


def _post(client: Client, name: str, data: dict[str, Any] | None = None, pk: int = 0) -> Any:
    url = reverse(f"admin:saved_filters_{name}", args=[pk] if pk else [])
    response = client.post(url, data or {})
    assert response.status_code == 200, response.content
    return json.loads(response.content)


def _save(client: Client, name: str, query: str, **extra: str) -> Any:
    return _post(client, "save", {"screen": SCREEN, "name": name, "query": query, **extra})


def _other_client(username: str = "kate") -> Client:
    """Второй сотрудник: видит «Площадки», но не суперпользователь."""
    user = User.objects.create_user(username, password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_productsitelatest"))
    client = Client()
    client.force_login(user)
    return client


# ---------- Строка адреса ----------


def test_clean_query_drops_page_service_marks_and_blanks() -> None:
    raw = "?status=new&p=3&q=&dr__range__gte=40&dr__range__lte=&e=1&_popup=1&o=2.-3"
    assert clean_query(raw) == "status=new&dr__range__gte=40&o=2.-3"


def test_same_query_ignores_order_and_page() -> None:
    assert same_query("a=1&b=2", "b=2&a=1&p=2")
    assert not same_query("a=1&b=2", "a=1")


# ---------- Сохранить ----------


def test_save_creates_set_and_returns_sets(admin_client: Client, admin_user: User) -> None:
    answer = _save(admin_client, "  Новые   EN ", "?status=new&p=2&language=en")
    assert answer["saved"] is True
    item = SavedFilter.objects.get()
    assert (item.user_id, item.screen, item.name) == (admin_user.pk, SCREEN, "Новые EN")
    # Номер страницы не сохраняется: набор открывается с первой.
    assert item.query == "status=new&language=en"
    assert answer["sets"] == [{"id": item.pk, "name": "Новые EN", "query": item.query}]
    assert answer["message"] == "Набор «Новые EN» сохранён."


def test_save_needs_name(admin_client: Client) -> None:
    answer = _save(admin_client, "   ", "status=new")
    assert answer == {"saved": False, "message": "Назовите набор."}
    assert not SavedFilter.objects.exists()


def test_save_same_name_asks_then_replaces(admin_client: Client) -> None:
    first = _save(admin_client, "Новые", "status=new")
    # Регистр не важен: «новые» — тот же набор.
    asked = _save(admin_client, "новые", "status=viewed")
    assert asked["saved"] is False and asked["exists"] is True
    assert SavedFilter.objects.get().query == "status=new"

    replaced = _save(admin_client, "новые", "status=viewed", replace="1")
    assert replaced["saved"] is True and replaced["id"] == first["id"]
    item = SavedFilter.objects.get()
    assert (item.name, item.query) == ("новые", "status=viewed")


def test_save_unknown_screen_is_404(admin_client: Client) -> None:
    url = reverse("admin:saved_filters_save")
    response = admin_client.post(url, {"screen": "sites.nothing", "name": "x", "query": ""})
    assert response.status_code == 404


def test_save_needs_list_permission(admin_client: Client) -> None:
    client = _other_client()
    url = reverse("admin:saved_filters_save")
    # «Позиции ключей» сотруднику не открыты — и набор для них не сохранить.
    response = client.post(url, {"screen": "keywords.keywordposition", "name": "x", "query": ""})
    assert response.status_code == 403


def test_save_only_post(admin_client: Client) -> None:
    assert admin_client.get(reverse("admin:saved_filters_save")).status_code == 405


# ---------- Удалить и вернуть ----------


def test_delete_marks_and_restore_returns(admin_client: Client) -> None:
    saved = _save(admin_client, "Новые", "status=new")
    deleted = _post(admin_client, "delete", pk=saved["id"])
    assert deleted["deleted"] is True and deleted["sets"] == []
    assert deleted["message"] == "Набор «Новые» удалён."
    # Не стёрт — помечен.
    assert SavedFilter.objects.get().deleted_at is not None

    restored = _post(admin_client, "restore", pk=saved["id"])
    assert restored["restored"] is True
    assert [item["id"] for item in restored["sets"]] == [saved["id"]]
    assert SavedFilter.objects.get().deleted_at is None


def test_deleted_name_is_free_and_restore_then_refuses(admin_client: Client) -> None:
    old = _save(admin_client, "Новые", "status=new")
    _post(admin_client, "delete", pk=old["id"])
    new = _save(admin_client, "Новые", "status=viewed")
    assert new["saved"] is True and new["id"] != old["id"]

    refused = _post(admin_client, "restore", pk=old["id"])
    assert refused["restored"] is False
    assert "уже сохранён заново" in refused["message"]
    assert SavedFilter.objects.get(pk=old["id"]).deleted_at is not None


def test_delete_twice_is_404(admin_client: Client) -> None:
    saved = _save(admin_client, "Новые", "status=new")
    _post(admin_client, "delete", pk=saved["id"])
    url = reverse("admin:saved_filters_delete", args=[saved["id"]])
    assert admin_client.post(url).status_code == 404


# ---------- Свои у каждого ----------


def test_sets_are_per_user(admin_client: Client) -> None:
    mine = _save(admin_client, "Новые", "status=new")
    kate = _other_client()
    # Тот же адрес — свой набор с тем же названием, не вопрос о замене.
    theirs = _save(kate, "Новые", "status=viewed")
    assert theirs["saved"] is True
    assert [item["id"] for item in theirs["sets"]] == [theirs["id"]]

    # Чужой набор не удалить и не вернуть.
    for action in ("delete", "restore"):
        url = reverse(f"admin:saved_filters_{action}", args=[mine["id"]])
        assert kate.post(url).status_code == 404
    assert SavedFilter.objects.get(pk=mine["id"]).deleted_at is None


# ---------- Блок над колонкой фильтров ----------


def _page(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def test_block_is_first_in_filter_column(admin_client: Client) -> None:
    html = _page(admin_client, reverse(LIST_URL))
    column = html.index('id="changelist-filter"')
    block = html.index('class="seo-saved"')
    header = html.index('id="changelist-filter-header"')
    assert column < block < header
    assert f'data-screen="{SCREEN}"' in html
    assert "Как в прошлый раз" in html


def test_block_lists_own_sets_and_marks_current(admin_client: Client) -> None:
    saved = _save(admin_client, "Новые", "status=new&language=en")
    other = _other_client()
    _save(other, "Чужой", "status=viewed")

    plain = _page(admin_client, reverse(LIST_URL))
    assert "Новые" in plain and "Чужой" not in plain
    assert f'<option value="{saved["id"]}" data-query="status=new&amp;language=en">' in plain

    # Адрес совпал с набором (порядок и страница не важны) — набор выбран.
    current = _page(admin_client, reverse(LIST_URL) + "?language=en&status=new&p=1")
    assert (
        f'<option value="{saved["id"]}" data-query="status=new&amp;language=en" selected>'
        in current
    )


@pytest.mark.parametrize(
    "url",
    [
        "admin:placements_placement_changelist",
        "admin:sites_site_changelist",
        "admin:keywords_keyword_changelist",
        "admin:observability_taskrun_changelist",
    ],
)
def test_block_on_other_lists_with_filters(admin_client: Client, url: str) -> None:
    assert 'class="seo-saved"' in _page(admin_client, reverse(url))


def test_no_block_without_filter_column(admin_client: Client) -> None:
    # У продавцов фильтров нет — нет и колонки, и блока.
    assert 'class="seo-saved"' not in _page(admin_client, reverse("admin:sites_seller_changelist"))


def test_no_block_in_popup(admin_client: Client) -> None:
    html = _page(admin_client, reverse("admin:sites_site_changelist") + "?_popup=1")
    assert 'class="seo-saved"' not in html
