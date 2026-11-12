"""Вход с кодом из Google Authenticator (ADR-081, config/otp.py)."""

import re
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.workspace import roles
from config import otp

pytestmark = pytest.mark.django_db

LOGIN = reverse("admin:login")
HOME = reverse("admin:index")
VERIFY = reverse("admin:otp_verify")
SETUP = reverse("admin:otp_setup")
USERS = reverse("admin:auth_user_changelist")
PASSWORD = "right-password-42"


@pytest.fixture(autouse=True)
def _required(settings: Any) -> None:
    settings.LOGIN_OTP_REQUIRED = True


@pytest.fixture
def boss() -> User:
    return User.objects.create_superuser("boss", password=PASSWORD)


def _password(client: Client, username: str = "boss") -> None:
    response = client.post(LOGIN, {"username": username, "password": PASSWORD, "next": HOME})
    assert response.status_code == 302


def _code(device: TOTPDevice) -> str:
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():06d}"


def _fresh(device: TOTPDevice) -> TOTPDevice:
    """Тот же код ещё раз в те же 30 секунд библиотека не примет — как будто их не было."""
    TOTPDevice.objects.filter(pk=device.pk).update(last_t=-1, throttling_failure_count=0)
    device.refresh_from_db()
    return device


def _linked(user: User) -> TOTPDevice:
    device = TOTPDevice.objects.create(user=user, name=otp.PHONE, confirmed=True)
    otp.new_backup_codes(user)
    return device


def _errors(response: Any) -> str:
    return " ".join(response.context["form"].errors.get("code", []))


class TestLocalVersion:
    def test_off_means_password_is_enough(self, settings: Any, boss: User) -> None:
        settings.LOGIN_OTP_REQUIRED = False
        client = Client()
        _password(client)
        assert client.get(HOME).status_code == 200
        assert client.get(VERIFY).status_code == 302


class TestSetup:
    def test_first_login_goes_to_setup(self, boss: User) -> None:
        client = Client()
        _password(client)
        response = client.get(HOME)
        assert response.status_code == 302
        assert response["Location"] == f"{VERIFY}?next=/admin/"
        response = client.get(response["Location"])
        assert response["Location"] == f"{SETUP}?next=/admin/"
        page = client.get(response["Location"])
        content = page.content.decode()
        assert "data:image/svg+xml;base64," in content
        assert page.context["secret"].replace(" ", "")
        assert "Выйти и войти под другим логином" in content
        # Меню до кода нет.
        assert 'id="nav-sidebar"' not in content

    def test_same_qr_after_reload(self, boss: User) -> None:
        client = Client()
        _password(client)
        first = client.get(SETUP).context["secret"]
        assert client.get(SETUP).context["secret"] == first
        assert TOTPDevice.objects.filter(user=boss).count() == 1

    def test_wrong_code_then_right_code_gives_backup_codes(self, boss: User) -> None:
        client = Client()
        _password(client)
        client.get(SETUP)
        device = TOTPDevice.objects.get(user=boss)
        response = client.post(SETUP, {"code": "000000", "next": HOME})
        assert "Код не подошёл" in _errors(response)
        assert not TOTPDevice.objects.get(pk=device.pk).confirmed
        device = _fresh(device)
        response = client.post(SETUP, {"code": _code(device), "next": HOME})
        assert response.status_code == 200
        codes = response.context["codes"]
        assert len(codes) == 10 and len(set(codes)) == 10
        assert all(re.fullmatch(r"[a-z2-7]{8}", code) for code in codes)
        assert "Скачать файлом" in response.content.decode()
        assert TOTPDevice.objects.get(pk=device.pk).confirmed
        assert StaticToken.objects.filter(device__user=boss).count() == 10
        assert client.get(HOME).status_code == 200

    def test_setup_asks_for_six_digits(self, boss: User) -> None:
        client = Client()
        _password(client)
        client.get(SETUP)
        response = client.post(SETUP, {"code": "abcdefgh", "next": HOME})
        assert "шесть цифр" in _errors(response)

    def test_linked_phone_cannot_be_replaced_without_reset(self, boss: User) -> None:
        _linked(boss)
        client = Client()
        _password(client)
        response = client.get(SETUP)
        assert response["Location"].startswith(VERIFY)
        assert TOTPDevice.objects.filter(user=boss).count() == 1


class TestVerify:
    def test_code_from_phone(self, boss: User) -> None:
        device = _linked(boss)
        client = Client()
        _password(client)
        assert client.get(HOME)["Location"] == f"{VERIFY}?next=/admin/"
        response = client.post(VERIFY, {"code": _code(device), "next": USERS})
        assert response.status_code == 302 and response["Location"] == USERS
        assert client.get(HOME).status_code == 200

    def test_code_with_space(self, boss: User) -> None:
        device = _linked(boss)
        client = Client()
        _password(client)
        code = _code(device)
        response = client.post(VERIFY, {"code": f"{code[:3]} {code[3:]}", "next": HOME})
        assert response.status_code == 302

    def test_wrong_code_then_wait(self, boss: User) -> None:
        _linked(boss)
        client = Client()
        _password(client)
        assert "Код не подошёл" in _errors(client.post(VERIFY, {"code": "000000"}))
        # Сразу же — даже верный код ждёт: неудачи подряд тормозят перебор.
        assert "Следующая попытка — через" in _errors(client.post(VERIFY, {"code": "000001"}))
        assert client.get(HOME).status_code == 302

    def test_backup_code_works_once(self, boss: User) -> None:
        _linked(boss)
        codes = otp.new_backup_codes(boss)
        first = Client()
        _password(first)
        response = first.post(VERIFY, {"code": codes[0].upper(), "next": HOME})
        assert response.status_code == 302
        assert first.get(HOME).status_code == 200
        assert otp.backup_left(boss) == 9
        second = Client()
        _password(second)
        assert "Каждый код действует один раз" in _errors(
            second.post(VERIFY, {"code": codes[0], "next": HOME})
        )

    def test_page_tells_backup_codes_left(self, boss: User) -> None:
        _linked(boss)
        client = Client()
        _password(client)
        assert "осталось 10" in client.get(VERIFY).content.decode()

    def test_next_to_other_site_ignored(self, boss: User) -> None:
        device = _linked(boss)
        client = Client()
        _password(client)
        response = client.post(VERIFY, {"code": _code(device), "next": "https://evil.example/"})
        assert response["Location"] == HOME

    def test_verified_session_skips_page(self, boss: User) -> None:
        device = _linked(boss)
        client = Client()
        _password(client)
        client.post(VERIFY, {"code": _code(device), "next": HOME})
        assert client.get(f"{VERIFY}?next={USERS}")["Location"] == USERS


class TestGate:
    def test_anonymous_goes_to_login(self) -> None:
        response = Client().get(VERIFY)
        assert response.status_code == 302 and response["Location"].startswith(LOGIN)

    def test_without_code_only_code_pages_and_logout(self, boss: User) -> None:
        _linked(boss)
        client = Client()
        _password(client)
        for path in (HOME, USERS, LOGIN, reverse("user_docs")):
            response = client.get(path)
            assert response.status_code == 302, path
            assert response["Location"].startswith(VERIFY), path
        assert client.get(reverse("healthz")).status_code == 200
        assert client.post(reverse("admin:otp_logout"))["Location"] == LOGIN
        assert client.get(HOME)["Location"].startswith(LOGIN)

    def test_admin_site_itself_requires_code(self, boss: User) -> None:
        """Защита админки — и без middleware: на случай забытого в нём адреса."""
        request = RequestFactory().get(HOME)
        boss.is_verified = lambda: False  # type: ignore[attr-defined]
        request.user = boss
        assert not admin.site.has_permission(request)
        boss.is_verified = lambda: True  # type: ignore[attr-defined]
        assert admin.site.has_permission(request)
        request.user = AnonymousUser()
        assert not admin.site.has_permission(request)

    def test_not_staff_cannot_use_code_pages(self) -> None:
        User.objects.create_user("guest", password=PASSWORD)
        client = Client()
        client.login(username="guest", password=PASSWORD)
        assert client.get(SETUP)["Location"].startswith(LOGIN)
        assert not TOTPDevice.objects.exists()


class TestReset:
    def _verified(self, user: User) -> Client:
        device = _fresh(TOTPDevice.objects.get(user=user, confirmed=True))
        client = Client()
        _password(client, user.username)
        assert client.post(VERIFY, {"code": _code(device), "next": HOME}).status_code == 302
        return client

    def test_admin_resets_lost_phone(self, boss: User) -> None:
        _linked(boss)
        anna = User.objects.create_user("anna", password=PASSWORD, is_staff=True)
        roles.set_role(anna, roles.LINKBUILDER)
        _linked(anna)
        anna_client = self._verified(anna)
        assert anna_client.get(HOME).status_code == 200
        boss_client = self._verified(boss)

        page = boss_client.get(reverse("admin:auth_user_change", args=[anna.pk]))
        assert "Телефон привязан" in page.content.decode()
        assert "запасных кодов осталось: 10" in page.content.decode()
        response = boss_client.post(
            reverse("admin:auth_user_change", args=[anna.pk]),
            {
                "first_name": "Анна",
                "last_name": "",
                "username": "anna",
                "email": "",
                "can_login": "on",
                "password1": "",
                "password2": "",
                "role": roles.LINKBUILDER,
                "otp_reset": "on",
            },
        )
        assert response.status_code == 302
        assert not TOTPDevice.objects.filter(user=anna).exists()
        assert not StaticDevice.objects.filter(user=anna).exists()
        # Сессия Анны теряет подтверждение сразу: дальше — привязка нового телефона.
        response = anna_client.get(HOME)
        assert response["Location"].startswith(VERIFY)
        assert anna_client.get(response["Location"])["Location"].startswith(SETUP)

    def test_nothing_to_reset_is_disabled(self, boss: User) -> None:
        _linked(boss)
        anna = User.objects.create_user("anna", is_staff=True)
        client = self._verified(boss)
        page = client.get(reverse("admin:auth_user_change", args=[anna.pk]))
        field = page.context["adminform"].form.fields["otp_reset"]
        assert field.disabled
        assert "привяжет его при первом входе" in field.help_text

    def test_list_shows_who_linked_phone(self, boss: User) -> None:
        _linked(boss)
        User.objects.create_user("anna", is_staff=True)
        client = self._verified(boss)
        content = client.get(USERS).content.decode()
        assert "телефон для кода" in content.lower()
        assert 'alt="True"' in content and 'alt="False"' in content
