"""Прод: всё из base плюс то, что нельзя забыть выставить.

HTTPS, HSTS и статика зависят от устройства VPS — задача E10-01.
"""

from config.settings.base import *  # noqa: F403

# Отладка в проде раскрывает настройки и код на странице ошибки —
# выключена независимо от DJANGO_DEBUG.
DEBUG = False

# Куки сессии и CSRF — только по HTTPS.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
