"""Пользователи в браузере (ADR-078): карточка панелью, роль кнопками, особые права.

Роль меняет таблицу прав на месте: «Администратор» — таблицы нет, другая роль —
её права отмечены серым; новый пароль — в той же форме. Серверную часть проверяет
tests/test_users_admin.py.
"""

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.workspace import roles

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"


def test_role_and_password_in_panel(admin_page: Page, live_server: LiveServer) -> None:
    User.objects.create_user("anna", first_name="Анна", is_staff=True)
    page = admin_page
    page.goto(f"{live_server.url}/admin/auth/user/")
    page.locator("#result_list a", has_text="Анна").first.click()
    panel = page.locator(PANEL)
    table = panel.locator("[data-perm-table]")
    expect(panel.locator("[data-role-hint=linkbuilder]")).to_be_hidden()

    panel.locator(".seo-choice label", has_text="Администратор").click()
    expect(table.locator("[data-perm-admin]")).to_be_visible()
    expect(panel.locator("[data-role-hint=admin]")).to_be_visible()
    expect(panel.locator("[data-role-hint=linkbuilder]")).to_be_hidden()

    panel.locator(".seo-choice label", has_text="Только просмотр").click()
    expect(table.locator("[data-perm-body]")).to_be_visible()
    expect(table.locator("td.seo-perm-role").first).to_be_visible()
    expect(table.locator("[data-perm-section]").first).to_contain_text("по роли:")

    panel.locator("input[name=password1]").fill("Linkbuilding-2026")
    panel.locator("input[name=password2]").fill("Linkbuilding-2026")
    panel.locator("[data-panel-save]").click()
    expect(panel).to_be_hidden()
    row = page.locator("#result_list tr", has_text="Анна")
    expect(row).to_contain_text("Только просмотр")
    anna = User.objects.get(username="anna")
    assert roles.role_of(anna) == roles.VIEWER and anna.check_password("Linkbuilding-2026")


def test_new_user_from_list(admin_page: Page, live_server: LiveServer) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/auth/user/")
    page.locator(".object-tools a", has_text="Добавить пользователя").click()
    panel = page.locator(PANEL)
    panel.locator("input[name=first_name]").fill("Борис")
    panel.locator("input[name=username]").fill("boris")
    panel.locator("input[name=password1]").fill("Linkbuilding-2026")
    panel.locator("input[name=password2]").fill("Linkbuilding-2026")
    panel.locator("[data-panel-save]").click()
    expect(page.locator("#result_list tr", has_text="Борис")).to_contain_text("Линкбилдер")
    boris = User.objects.get(username="boris")
    assert boris.is_staff and roles.role_of(boris) == roles.LINKBUILDER
