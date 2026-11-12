"""Вход с кодом из Google Authenticator (E10-04, ADR-081).

На сервере после пароля система спрашивает шесть цифр из приложения на
телефоне или один из запасных кодов. Первый вход после включения — привязка
телефона: QR-код, проверочный код, десять запасных кодов, которые показываются
один раз. Потерял телефон — администратор отмечает «Сбросить Authenticator» в
карточке пользователя, и при следующем заходе человек привязывает новый.
В локальной версии выключено (`LOGIN_OTP_REQUIRED`).

Телефон и запасные коды хранит django-otp. Его `OTPMiddleware` кладёт в
`request.user.otp_device` то, чем подтверждён вход в этой сессии, или None.
Подтверждение живёт в сессии вместе с паролем — 14 дней от последнего захода
(ADR-081), код не спрашивается на каждом входе в систему с того же браузера.

Неверные коды тормозит сама библиотека: после каждой неудачи подряд следующая
попытка — через 1, 2, 4, 8… секунд, счёт свой у телефона и у запасных кодов.
"""

import base64
import io
import logging
import math
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

logger = logging.getLogger(__name__)

PHONE = "Google Authenticator"
BACKUP = "Запасные коды"
# Код из приложения — шесть цифр; запасной — восемь букв и цифр.
PHONE_CODE = re.compile(r"\d{6}")


# --- Телефон и запасные коды ---------------------------------------------------


def phone(user: Any) -> TOTPDevice | None:
    """Привязанный телефон человека; не привязан — None."""
    device: TOTPDevice | None = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    return device


def backup_left(user: Any) -> int:
    """Сколько запасных кодов у человека ещё не использовано."""
    return int(StaticToken.objects.filter(device__user=user, device__confirmed=True).count())


def new_backup_codes(user: Any) -> list[str]:
    """Новый набор запасных кодов вместо прежнего; показать их можно только сейчас."""
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name=BACKUP, confirmed=True)
    codes = [StaticToken.random_token() for _ in range(settings.LOGIN_OTP_BACKUP_CODES)]
    StaticToken.objects.bulk_create(StaticToken(device=device, token=code) for code in codes)
    return codes


def reset(user: Any) -> int:
    """Отвязать телефон и запасные коды. Сессии человека сразу теряют
    подтверждение: при следующем заходе он привяжет телефон заново."""
    phones, _ = TOTPDevice.objects.filter(user=user).delete()
    backups, _ = StaticDevice.objects.filter(user=user).delete()
    logger.info("Authenticator сброшен", extra={"user_id": user.pk})
    return int(phones) + int(backups)


def is_verified(user: Any) -> bool:
    """Вход подтверждён кодом в этой сессии (ставит `OTPMiddleware`)."""
    check: Callable[[], bool] | None = getattr(user, "is_verified", None)
    return bool(check and check())


def _secret_text(device: TOTPDevice) -> str:
    """Ключ для ручного ввода в приложение — base32 группами по четыре."""
    key = base64.b32encode(device.bin_key).decode()
    return " ".join(key[start : start + 4] for start in range(0, len(key), 4))


def _qr_data_uri(device: TOTPDevice) -> str:
    """QR-код привязки картинкой SVG: в тёмной расцветке — на белой подложке."""
    image = qrcode.make(device.config_url, image_factory=qrcode.image.svg.SvgPathImage)
    out = io.BytesIO()
    image.save(out)
    return "data:image/svg+xml;base64," + base64.b64encode(out.getvalue()).decode()


def check_code(device: Any, code: str) -> str:
    """Проверить код устройством; пустая строка — подошёл, иначе — что сказать."""
    with transaction.atomic():
        locked = type(device).objects.select_for_update().get(pk=device.pk)
        allowed, info = locked.verify_is_allowed()
        if not allowed:
            return _wait_text(info)
        if locked.verify_token(code):
            return ""
    if isinstance(device, StaticDevice):
        return "Запасной код не подошёл. Каждый код действует один раз."
    return (
        "Код не подошёл. Введите свежий код из приложения; если коды не подходят"
        " подряд — проверьте, что время на телефоне выставляется автоматически."
    )


def _wait_text(info: dict[str, Any] | None) -> str:
    until = (info or {}).get("locked_until")
    seconds = max(1, math.ceil((until - timezone.now()).total_seconds())) if until else 1
    wait = f"{seconds} с" if seconds < 90 else f"{math.ceil(seconds / 60)} мин"
    return f"Слишком много неверных кодов подряд. Следующая попытка — через {wait}."


# --- Без кода — только страницы кода -----------------------------------------


class RequireOTPMiddleware:
    """После `OTPMiddleware`: вошедший по паролю без кода видит только страницы
    кода и выход, всё остальное ведёт на код. Защиту админки повторяет
    `SeoAdminSite.has_permission` — на случай адреса, забытого здесь."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user
        if (
            settings.LOGIN_OTP_REQUIRED
            and user.is_authenticated
            and not is_verified(user)
            and not _open_without_code(request.path)
        ):
            return HttpResponseRedirect(_with_next("admin:otp_verify", request.get_full_path()))
        return self.get_response(request)


def _open_without_code(path: str) -> bool:
    open_paths = {
        reverse("admin:otp_verify"),
        reverse("admin:otp_setup"),
        reverse("admin:otp_logout"),
        reverse("healthz"),
    }
    return path in open_paths or path.startswith("/" + settings.STATIC_URL.lstrip("/"))


def _with_next(name: str, target: str) -> str:
    return f"{reverse(name)}?next={quote(target)}"


# --- Страницы кода ----------------------------------------------------------


class CodeForm(forms.Form):
    code = forms.CharField(
        label="Код",
        max_length=32,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "autofocus": True,
                "autocapitalize": "off",
                "spellcheck": "false",
                "class": "seo-otp-code",
            }
        ),
    )

    def clean_code(self) -> str:
        # «123 456» из приложения и «Abcd 2345» из файла — без пробелов.
        return re.sub(r"\s+", "", str(self.cleaned_data["code"])).lower()


def _target(request: HttpRequest) -> str:
    target = str(request.POST.get("next") or request.GET.get("next") or "")
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return reverse("admin:index")
    return target


def _staff_only(request: HttpRequest) -> HttpResponse | None:
    """Страницы кода — только сотруднику, который уже ввёл пароль."""
    user = request.user
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return redirect_to_login(request.get_full_path(), reverse("admin:login"))
    return None


def _page(request: HttpRequest, template: str, **context: Any) -> TemplateResponse:
    return TemplateResponse(
        request,
        f"admin/otp/{template}",
        {**admin.site.each_context(request), **context},
    )


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    """Выйти со страницы кода. Штатный выход админки того, у кого нет доступа
    (а без кода его нет), не выпускает, а отправляет на главную."""
    logout(request)
    return redirect("admin:login")


@never_cache
@sensitive_post_parameters("code")
def verify_view(request: HttpRequest) -> HttpResponse:
    """Код из приложения или запасной код после пароля."""
    if denied := _staff_only(request):
        return denied
    target = _target(request)
    user = request.user
    if not settings.LOGIN_OTP_REQUIRED or is_verified(user):
        return redirect(target)
    device = phone(user)
    if device is None:
        return redirect(_with_next("admin:otp_setup", target))
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = form.cleaned_data["code"]
        chosen: Any = device
        if not PHONE_CODE.fullmatch(code):
            chosen = StaticDevice.objects.filter(user=user, confirmed=True).first()
        if chosen is None:
            form.add_error("code", "Запасных кодов нет. Введите шесть цифр из приложения.")
        elif error := check_code(chosen, code):
            logger.warning("Код при входе не подошёл", extra={"user_id": user.pk})
            form.add_error("code", error)
        else:
            otp_login(request, chosen)
            kind = "запасной код" if isinstance(chosen, StaticDevice) else "приложение"
            logger.info("Вход подтверждён кодом", extra={"user_id": user.pk, "kind": kind})
            return redirect(target)
    return _page(
        request,
        "verify.html",
        title="Код из приложения",
        form=form,
        next=target,
        backup_left=backup_left(user),
    )


@never_cache
@sensitive_post_parameters("code")
def setup_view(request: HttpRequest) -> HttpResponse:
    """Привязка телефона: QR-код, проверочный код, запасные коды один раз."""
    if denied := _staff_only(request):
        return denied
    target = _target(request)
    user = request.user
    if not settings.LOGIN_OTP_REQUIRED:
        return redirect(target)
    if phone(user) is not None:
        # Привязан — новый привязать можно только после сброса администратором.
        return redirect(_with_next("admin:otp_verify", target))
    # Тот же QR, пока человек не закончил: обновил страницу после
    # сканирования — запись в приложении остаётся верной.
    device = TOTPDevice.objects.filter(user=user, confirmed=False).order_by("pk").first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name=PHONE, confirmed=False)
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = form.cleaned_data["code"]
        error = (
            check_code(device, code)
            if PHONE_CODE.fullmatch(code)
            else "Введите шесть цифр, которые показывает приложение."
        )
        if error:
            form.add_error("code", error)
        else:
            with transaction.atomic():
                TOTPDevice.objects.filter(user=user).exclude(pk=device.pk).delete()
                device.confirmed = True
                device.save(update_fields=["confirmed"])
                codes = new_backup_codes(user)
            otp_login(request, device)
            logger.info("Телефон привязан", extra={"user_id": user.pk})
            return _page(request, "codes.html", title="Запасные коды", codes=codes, next=target)
    return _page(
        request,
        "setup.html",
        title="Привяжите телефон",
        form=form,
        next=target,
        qr=_qr_data_uri(device),
        secret=_secret_text(device),
        account=user.get_username(),
    )
