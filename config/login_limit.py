"""Ограничение попыток входа (E10-04, ADR-081).

Система на сервере открыта в интернет, а адрес с сертификатом попадает в
публичные журналы сертификатов — боты пробуют пароли. После
`LOGIN_FAIL_LIMIT` неверных паролей подряд вход для этого логина с этого
адреса закрыт на `LOGIN_FAIL_MINUTES` минут после последней неудачи — даже с
верным паролем, иначе перебор шёл бы дальше. Удачный вход счёт обнуляет.

Счёт — в кеше Redis: переживает перезапуск приложения, общий у всех процессов
gunicorn. Ключ — логин и адрес вместе: чужой перебор с другого адреса не
закрывает вход хозяину логина.
"""

import hashlib
import logging
from typing import Any

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest

logger = logging.getLogger(__name__)


def client_ip(request: HttpRequest) -> str:
    """Адрес человека: за прокси (Caddy в проде) — из X-Forwarded-For.

    Caddy заголовок от клиента не принимает, а ставит сам: последний адрес в
    нём — тот, с кого пришёл запрос. Без прокси заголовку верить нельзя —
    его может подставить кто угодно.
    """
    if settings.BEHIND_PROXY:
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", ""))
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return str(request.META.get("REMOTE_ADDR", ""))


def _key(request: HttpRequest, username: str) -> str:
    # Логин — что угодно из формы: в ключ идёт его отпечаток, а не он сам.
    login = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:32]
    return f"login-fail:{client_ip(request)}:{login}"


TOO_MANY = "Слишком много неверных попыток входа. Попробуйте через %(minutes)s мин."


class LimitedLoginForm(AdminAuthenticationForm):
    """Форма входа админки со счётчиком неверных паролей."""

    def clean(self) -> dict[str, Any]:
        username = str(self.cleaned_data.get("username") or "")
        if self.request is None or not username:
            return super().clean()
        key = _key(self.request, username)
        minutes = settings.LOGIN_FAIL_MINUTES
        if int(cache.get(key, 0)) >= settings.LOGIN_FAIL_LIMIT:
            raise ValidationError(
                TOO_MANY,
                code="too_many_attempts",
                params={"minutes": minutes},
            )
        try:
            cleaned = super().clean()
        except ValidationError as error:
            if error.code == "invalid_login":
                timeout = minutes * 60
                cache.add(key, 0, timeout)
                failures = cache.incr(key)
                # Окно отсчитывается от последней неудачи, а не от первой.
                cache.touch(key, timeout)
                if failures == settings.LOGIN_FAIL_LIMIT:
                    logger.warning(
                        "Вход закрыт после неверных паролей",
                        extra={"ip": client_ip(self.request), "failures": failures},
                    )
            raise
        cache.delete(key)
        return cleaned
