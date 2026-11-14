"""Вход по логину или по почте (пользователь, 14.11.2026; ADR-078).

В поле «Логин или почта» на странице входа можно вписать любое из двух. Почта
сверяется без учёта регистра и только если она одна у одного пользователя:
одинаковые почты у двух людей карточка пользователя не даёт завести
(apps/workspace/admin.py), а на случай старых данных — по такой почте не пускаем.
Дальше всё как раньше: пароль, ограничение попыток (config/login_limit.py), код
из телефона (config/otp.py).
"""

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.http import HttpRequest


def user_by_login(login: str) -> Any | None:
    """Пользователь по логину, а если такого логина нет — по почте; иначе None."""
    model = get_user_model()
    login = login.strip()
    if not login:
        return None
    found = model._default_manager.filter(username=login).first()
    if found is not None or "@" not in login:
        return found
    by_email = list(model._default_manager.filter(email__iexact=login)[:2])
    return by_email[0] if len(by_email) == 1 else None


class LoginOrEmailBackend(ModelBackend):
    """Как штатный вход Django, но имя пользователя может быть и почтой."""

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        if username is None or password is None:
            return None
        user = user_by_login(username)
        if user is None:
            # Как у ModelBackend: время ответа то же, что с неверным паролем, —
            # по нему не узнать, есть ли такой логин.
            get_user_model()().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            found: User = user
            return found
        return None
