"""Вход по логину или по почте (config/auth.py)."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.workspace import roles

pytestmark = pytest.mark.django_db

LOGIN = reverse("admin:login")
HOME = reverse("admin:index")
PASSWORD = "right-password-42"


@pytest.fixture
def anna() -> User:
    user = User.objects.create_user(
        "anna", email="Anna@Example.com", password=PASSWORD, is_staff=True
    )
    roles.set_role(user, roles.LINKBUILDER)
    return user


def _login(login: str, password: str = PASSWORD, client: Client | None = None) -> bool:
    client = client or Client()
    response = client.post(LOGIN, {"username": login, "password": password, "next": HOME})
    return response.status_code == 302


class TestLogin:
    def test_by_username(self, anna: User) -> None:
        assert _login("anna")

    def test_by_email_any_case(self, anna: User) -> None:
        assert _login("anna@example.com")
        assert _login(" ANNA@EXAMPLE.COM ")

    def test_wrong_password(self, anna: User) -> None:
        assert not _login("anna@example.com", "wrong")

    def test_not_active(self, anna: User) -> None:
        User.objects.filter(pk=anna.pk).update(is_active=False)
        assert not _login("anna@example.com")

    def test_shared_email_is_not_a_login(self, anna: User) -> None:
        User.objects.create_user("anna2", email="anna@example.com", password=PASSWORD)
        assert not _login("anna@example.com")
        assert _login("anna")

    def test_label(self) -> None:
        assert "Логин или почта" in Client().get(LOGIN).content.decode()

    def test_one_failure_count_for_login_and_email(self, anna: User, settings: Any) -> None:
        settings.LOGIN_FAIL_LIMIT = 3
        client = Client()
        for login in ("anna", "anna@example.com", "ANNA"):
            assert not _login(login, "wrong", client)
        # Три неверных — по логину и по почте вместе: вход закрыт и с верным паролем.
        assert not _login("anna@example.com", client=client)


class TestUserCard:
    def test_email_must_be_unique(self, admin_client: Client, anna: User) -> None:
        response = admin_client.post(
            reverse("admin:auth_user_add"),
            {
                "first_name": "Борис",
                "last_name": "",
                "username": "boris",
                "email": "ANNA@example.com",
                "can_login": "on",
                "password1": "Linkbuilding-2026",
                "password2": "Linkbuilding-2026",
                "role": roles.LINKBUILDER,
            },
        )
        assert response.status_code == 200
        assert "уже у другого пользователя" in response.content.decode()
        assert not User.objects.filter(username="boris").exists()

    def test_own_email_kept(self, admin_client: Client, anna: User) -> None:
        response = admin_client.post(
            reverse("admin:auth_user_change", args=[anna.pk]),
            {
                "first_name": "Анна",
                "last_name": "",
                "username": "anna",
                "email": "anna@example.com",
                "can_login": "on",
                "password1": "",
                "password2": "",
                "role": roles.LINKBUILDER,
            },
        )
        assert response.status_code == 302
