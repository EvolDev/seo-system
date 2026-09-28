"""Локальная разработка: всё из base, DEBUG — из DJANGO_DEBUG."""

from django.conf import settings
from django.http import HttpRequest

from config.logs import logging_config
from config.settings.base import *  # noqa: F403

# Логи читаются глазами: строка вместо JSON. Посмотреть JSON — "json".
LOGGING = logging_config("console")

# Счётчик SQL-запросов на странице (ADR-003): панель справа при DJANGO_DEBUG.
# Приложение в контейнере видит запросы с адреса шлюза Docker, а не с
# 127.0.0.1, поэтому INTERNAL_IPS не подходит — решает DEBUG. Читаем его в
# момент запроса: в тестах pytest-django выключает DEBUG, и панели нет.
INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]  # noqa: F405
MIDDLEWARE = ["debug_toolbar.middleware.DebugToolbarMiddleware", *MIDDLEWARE]  # noqa: F405
DEBUG_TOOLBAR_CONFIG = {
    "SHOW_TOOLBAR_CALLBACK": "config.settings.local.show_toolbar",
    # Свёрнута в ярлычок у края: развёрнутая закрывала шапку и переключатели (E9-08).
    "SHOW_COLLAPSED": True,
}


def show_toolbar(request: HttpRequest) -> bool:
    return bool(settings.DEBUG)
