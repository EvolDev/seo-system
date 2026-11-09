"""Сессия кончается через `SESSION_DAYS` дней без захода, а не после входа (ADR-081).

Django считает срок сессии от последнего сохранения, а сохраняет её, только
когда она изменилась: человека, который заходит каждый день, раз в две недели
выкидывало бы на вход. Раз в день отметка дня в сессии меняется — сессия
сохраняется, и её срок отсчитывается заново. Чаще не надо: запись в базу на
каждый запрос (`SESSION_SAVE_EVERY_REQUEST`) — лишняя работа ради того же.
"""

from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone

KEY = "seen_on"


def mark_login(sender: Any, request: HttpRequest | None, user: Any, **kwargs: Any) -> None:
    """Вход — тоже заход: отметка дня сохраняется вместе с сессией входа.

    Иначе первый же запрос после входа сохранял бы сессию ещё раз. Подключает
    `SeoAdminConfig.ready` к сигналу `user_logged_in`.
    """
    if request is not None and hasattr(request, "session"):
        request.session[KEY] = timezone.localdate().isoformat()


class SessionRefreshMiddleware:
    """После `AuthenticationMiddleware`: продлевает сессию вошедшего раз в день."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            today = timezone.localdate().isoformat()
            if request.session.get(KEY) != today:
                request.session[KEY] = today
        return self.get_response(request)
