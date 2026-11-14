"""Тесты: всё из локальных настроек, адреса — без секретного пути (ADR-081).

Тесты пишут адреса прямо — `/admin/…`, `/docs/…`. Путь из `.env` (`SITE_PATH`)
локальной версии им не мешает: здесь он всегда пуст. Раскладку под путём
проверяет tests/test_site_path.py.
"""

from config.settings.local import *  # noqa: F403

SITE_PATH = ""
URL_ROOT = ""
STATIC_URL = "static/"
