"""Ограничение попыток входа и срок сессии (ADR-081)."""

from datetime import date
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from config.session_refresh import KEY

pytestmark = pytest.mark.django_db

LOGIN = reverse("admin:login")
HOME = reverse("admin:index")


@pytest.fixture
def boss() -> User:
    return User.objects.create_superuser("boss", password="right-password-42")


def _login(client: Client, password: str, **headers: Any) -> str:
    response = client.post(
        LOGIN, {"username": "boss", "password": password, "next": HOME}, **headers
    )
    if response.status_code == 302:
        return "вошёл"
    errors = response.context["form"].non_field_errors()
    return str(errors[0])


class TestLoginLimit:
    @pytest.fixture(autouse=True)
    def limit(self, settings: Any) -> None:
        settings.LOGIN_FAIL_LIMIT = 3
        settings.LOGIN_FAIL_MINUTES = 15

    def test_closed_after_limit_even_with_right_password(self, boss: User) -> None:
        client = Client()
        for _ in range(3):
            assert "корректные имя пользователя и пароль" in _login(client, "wrong")
        message = _login(client, "right-password-42")
        assert message == "Слишком много неверных попыток входа. Попробуйте через 15 мин."

    def test_success_resets_count(self, boss: User) -> None:
        client = Client()
        _login(client, "wrong")
        _login(client, "wrong")
        assert _login(client, "right-password-42") == "вошёл"
        client.logout()
        _login(client, "wrong")
        _login(client, "wrong")
        assert _login(client, "right-password-42") == "вошёл"

    def test_other_address_not_closed(self, boss: User) -> None:
        for _ in range(3):
            _login(Client(REMOTE_ADDR="10.0.0.1"), "wrong")
        assert "Слишком много" in _login(Client(REMOTE_ADDR="10.0.0.1"), "right-password-42")
        assert _login(Client(REMOTE_ADDR="10.0.0.2"), "right-password-42") == "вошёл"

    def test_forwarded_header_ignored_without_proxy(self, boss: User) -> None:
        """Локально прокси нет: подставленный X-Forwarded-For счёт не обходит."""
        for number in range(3):
            _login(Client(), "wrong", HTTP_X_FORWARDED_FOR=f"10.0.0.{number}")
        assert "Слишком много" in _login(Client(), "right-password-42")

    def test_behind_proxy_counts_by_client_address(self, boss: User, settings: Any) -> None:
        """За Caddy адрес человека — последний в X-Forwarded-For."""
        settings.BEHIND_PROXY = True
        for _ in range(3):
            _login(Client(), "wrong", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.1")
        assert "Слишком много" in _login(
            Client(), "right-password-42", HTTP_X_FORWARDED_FOR="10.0.0.1"
        )
        assert _login(Client(), "right-password-42", HTTP_X_FORWARDED_FOR="10.0.0.2") == "вошёл"


class TestSessionRefresh:
    def test_session_renewed_once_a_day(self, admin_user: User) -> None:
        client = Client()
        day_one, day_two = date(2026, 11, 9), date(2026, 11, 10)
        with mock.patch("config.session_refresh.timezone.localdate", return_value=day_one):
            client.force_login(admin_user)
            # Вход уже отметил день: в тот же день сессия не сохраняется заново.
            assert client.session[KEY] == "2026-11-09"
            same_day = client.get(HOME)
        with mock.patch("config.session_refresh.timezone.localdate", return_value=day_two):
            next_day = client.get(HOME)
        # Новый день — сессия сохранена, и куки с новым сроком ушли в браузер.
        assert "sessionid" not in same_day.cookies
        assert next_day.cookies["sessionid"]["max-age"] == 14 * 24 * 60 * 60
        assert client.session[KEY] == "2026-11-10"

    def test_anonymous_session_untouched(self, client: Client) -> None:
        response = client.get(LOGIN)
        assert "sessionid" not in response.cookies


def test_healthz_without_login(client: Client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"
