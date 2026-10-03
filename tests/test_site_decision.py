"""«Решение по площадке» в «Площадках» (E9-09, E9-11): статус без перехода.

Панель справа (seo/panel.js) берёт форму и отправляет её с заголовком
X-Seo-Partial; браузерную часть проверяет tests/e2e/test_panel.py.
"""

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.sites.models import Product, ProductSite, Site, SiteList, SiteListItem, SiteStatus

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}


@pytest.fixture
def row() -> ProductSite:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="coingabbar.com")
    decision = ProductSite.objects.get(site=site, product=product)
    decision.imported_undecided = True
    decision.save(update_fields=["imported_undecided"])
    return decision


def _url(row: ProductSite) -> str:
    return reverse("admin:sites_productsite_decision", args=[row.pk])


def test_sites_list_status_opens_decision_panel(admin_client: Client, row: ProductSite) -> None:
    site_list = SiteList.objects.create(name="Октябрь")
    SiteListItem.objects.create(site_list=site_list, site=row.site)
    page = admin_client.get(reverse("admin:sites_productsitelatest_changelist")).content.decode()
    # Щелчок — панель с решением, Ctrl и без скрипта — полная форма.
    full = reverse("admin:sites_productsite_change", args=[row.pk])
    assert f'<a href="{full}" data-panel="{_url(row)}" title="Сменить статус">Новая</a>' in page
    assert "seo/panel.js" in page


def test_panel_gets_only_the_form(admin_client: Client, row: ProductSite) -> None:
    response = admin_client.get(_url(row), headers=PARTIAL)
    page = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in page
    assert '<h2 class="seo-panel-title">coingabbar.com · Convertio</h2>' in page
    # Статус — кнопками; рядом — карточка той же площадки в той же панели.
    assert 'type="radio" name="status"' in page and 'name="reject_reason"' in page
    card = reverse("admin:sites_site_card", args=[row.site_id])
    assert f'<a href="{card}" data-panel>Карточка площадки</a>' in page
    assert "data-panel-save" in page
    assert "seo/widgets.css" in page.split("<header")[0]  # стили кнопок — до содержимого
    assert "Импортирована без решения" in page


def test_without_window_opens_full_form(admin_client: Client, row: ProductSite) -> None:
    response = admin_client.get(_url(row))
    assert response["Location"] == reverse("admin:sites_productsite_change", args=[row.pk])


def test_save_status_and_reason(admin_client: Client, row: ProductSite) -> None:
    response = admin_client.post(
        _url(row),
        {"status": SiteStatus.DISCARDED, "reject_reason": "Nofollow"},
        headers=PARTIAL,
    )
    assert response.json() == {
        "saved": True,
        "message": "coingabbar.com · Convertio — Отбрасываю",
    }
    row.refresh_from_db()
    assert row.status == SiteStatus.DISCARDED
    assert row.reject_reason == "Nofollow"
    # Решение принято — пометка «без решения» снята; причина — в истории заметок.
    assert not row.imported_undecided
    assert list(row.site.notes.values_list("body", flat=True)) == ["Nofollow"]


def test_wrong_status_shows_form_with_error(admin_client: Client, row: ProductSite) -> None:
    response = admin_client.post(_url(row), {"status": "New", "reject_reason": ""}, headers=PARTIAL)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert "errorlist" in response.content.decode()
    row.refresh_from_db()
    assert row.status == SiteStatus.NEW


def test_view_only_user_cannot_change(client: Client, row: ProductSite) -> None:
    user = User.objects.create_user("viewer", password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_productsite"))
    client.force_login(user)
    response = client.post(_url(row), {"status": SiteStatus.APPROVED}, headers=PARTIAL)
    assert response.status_code == 403
    row.refresh_from_db()
    assert row.status == SiteStatus.NEW
