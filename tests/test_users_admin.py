"""Пользователи: роли, карточка, список (ADR-078)."""

import json

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.test import Client
from django.urls import reverse

from apps.workspace import roles

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}
USERS = reverse("admin:auth_user_changelist")
ADD = reverse("admin:auth_user_add")


def _codes(role: str) -> set[str]:
    return {
        f"{p.content_type.app_label}.{p.codename}"
        for p in Permission.objects.filter(group__name=roles.GROUPS[role]).select_related(
            "content_type"
        )
    }


def _form(**fields: str) -> dict[str, str]:
    data = {
        "first_name": "Анна",
        "last_name": "",
        "username": "anna",
        "email": "",
        "can_login": "on",
        "password1": "Linkbuilding-2026",
        "password2": "Linkbuilding-2026",
        "role": roles.LINKBUILDER,
    }
    data.update(fields)
    return data


def _change(user: User) -> str:
    return reverse("admin:auth_user_change", args=[user.pk])


class TestRoles:
    def test_groups_follow_code_after_migrate(self) -> None:
        linkbuilder = _codes(roles.LINKBUILDER)
        assert {
            "placements.change_placement",
            "sites.change_site",
            "sites.delete_upload",
            "keywords.add_keyword",
            "content.change_copywriter",
            "content.view_domainsetting",
            "observability.view_taskrun",
            "workspace.add_savedfilter",
        } <= linkbuilder
        assert "content.change_domainsetting" not in linkbuilder
        assert "observability.change_taskrun" not in linkbuilder
        system = ("auth.", "admin.", "sessions.")
        assert not {code for code in linkbuilder if code.startswith(system)}
        viewer = _codes(roles.VIEWER)
        assert "placements.view_placement" in viewer and "workspace.change_usersettings" in viewer
        assert not {
            code for code in viewer if not code.startswith("workspace.") and ".view_" not in code
        }

    def test_sync_restores_changed_group(self) -> None:
        group = Group.objects.get(name=roles.GROUPS[roles.LINKBUILDER])
        group.permissions.clear()
        roles.sync()
        assert "placements.change_placement" in _codes(roles.LINKBUILDER)

    def test_set_role_and_role_of(self) -> None:
        user = User.objects.create_user("lilith")
        roles.set_role(user, roles.VIEWER)
        assert roles.role_of(user) == roles.VIEWER and not user.is_superuser
        roles.set_role(user, roles.ADMIN)
        assert roles.role_of(user) == roles.ADMIN and not user.groups.exists()
        roles.set_role(user, roles.NONE)
        user.refresh_from_db()
        assert roles.role_of(user) == roles.NONE and not user.is_superuser


class TestCard:
    def test_new_user_with_role_and_password(self, admin_client: Client) -> None:
        response = admin_client.post(ADD, _form(), headers=PARTIAL)
        assert json.loads(response.content)["saved"]
        user = User.objects.get(username="anna")
        assert user.is_staff and user.is_active and not user.is_superuser
        assert user.check_password("Linkbuilding-2026")
        assert roles.role_of(user) == roles.LINKBUILDER
        assert user.has_perm("placements.change_placement")
        assert not user.has_perm("auth.change_user")

    def test_employee_without_login(self, admin_client: Client) -> None:
        admin_client.post(
            ADD,
            _form(username="artem", can_login="", password1="", password2="", role=""),
            headers=PARTIAL,
        )
        user = User.objects.get(username="artem")
        assert not user.is_staff and not user.has_usable_password()

    def test_login_needs_password(self, admin_client: Client) -> None:
        response = admin_client.post(ADD, _form(password1="", password2=""), headers=PARTIAL)
        assert "Задайте пароль — без него войти нельзя." in response.content.decode()
        assert not User.objects.filter(username="anna").exists()

    def test_passwords_must_match(self, admin_client: Client) -> None:
        response = admin_client.post(ADD, _form(password2="другой-пароль"), headers=PARTIAL)
        assert "Пароли не совпадают." in response.content.decode()

    def test_change_role_keeps_password(self, admin_client: Client) -> None:
        user = User.objects.create_user("anna", password="Linkbuilding-2026", is_staff=True)
        response = admin_client.post(
            _change(user), _form(password1="", password2="", role=roles.ADMIN), headers=PARTIAL
        )
        assert json.loads(response.content)["saved"]
        user.refresh_from_db()
        assert user.is_superuser and user.check_password("Linkbuilding-2026")

    def test_own_permissions_besides_role(self, admin_client: Client) -> None:
        user = User.objects.create_user("anna", password="Linkbuilding-2026", is_staff=True)
        extra = Permission.objects.get(codename="change_domainsetting")
        admin_client.post(
            _change(user),
            {
                **_form(password1="", password2="", role=roles.VIEWER),
                "user_permissions": [extra.pk],
            },
            headers=PARTIAL,
        )
        user.refresh_from_db()
        assert roles.role_of(user) == roles.VIEWER
        assert list(user.user_permissions.all()) == [extra]

    def test_cannot_lock_oneself_out(self, admin_client: Client, admin_user: User) -> None:
        response = admin_client.post(
            _change(admin_user),
            _form(username=admin_user.username, password1="", password2="", role=roles.VIEWER),
            headers=PARTIAL,
        )
        assert "Себе снять полный доступ нельзя" in response.content.decode()
        admin_user.refresh_from_db()
        assert admin_user.is_superuser

    def test_permissions_grouped_in_russian(self, admin_client: Client) -> None:
        user = User.objects.create_user("anna", is_staff=True)
        page = admin_client.get(_change(user), headers=PARTIAL).content.decode()
        for title in ("Работа", "Справочники", "Настройки", "Служебное", "Данные под экранами"):
            assert f'<span class="seo-perms-title">{title}</span>' in page
        assert "Can add" not in page
        assert "data-role-perms=" in page and "Вход в систему" in page


class TestList:
    def test_role_column_and_filter(self, admin_client: Client) -> None:
        anna = User.objects.create_user("anna", first_name="Анна", is_staff=True)
        roles.set_role(anna, roles.LINKBUILDER)
        User.objects.create_user("artem")
        response = admin_client.get(USERS, {"role": roles.LINKBUILDER})
        assert [user.username for user in response.context["cl"].result_list] == ["anna"]
        assert "Линкбилдер" in response.content.decode()
        response = admin_client.get(USERS, {"login": "no"})
        assert [user.username for user in response.context["cl"].result_list] == ["artem"]

    def test_no_groups_screen(self, admin_client: Client) -> None:
        assert admin_client.get("/admin/auth/group/").status_code == 404
