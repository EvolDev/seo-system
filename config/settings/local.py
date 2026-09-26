"""Локальная разработка: всё из base, DEBUG — из DJANGO_DEBUG."""

from config.logs import logging_config
from config.settings.base import *  # noqa: F403

# Логи читаются глазами: строка вместо JSON. Посмотреть JSON — "json".
LOGGING = logging_config("console")
