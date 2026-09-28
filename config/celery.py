"""Приложение Celery: очередь фоновых задач (E2-01, ADR-039).

Воркер (`celery -A config worker`) выполняет задачи, beat
(`celery -A config beat`) по расписанию кладёт их в очередь. Настройки —
из Django: всё, что в `config/settings` начинается с `CELERY_`. Задачи
ищутся в модулях `tasks.py` приложений из INSTALLED_APPS.

Базовый класс задач — `config/queue.py`: `run_id`, журнал запусков,
повторы, алерт, ограничение скорости.
"""

import os
from typing import Any

from celery import Celery
from celery.signals import setup_logging

from config.env import settings_module

# Воркер и beat запускает команда celery, а не manage.py: модуль настроек
# Django выбираем здесь так же. Уже выбран (manage.py, pytest) — не трогаем.
if "DJANGO_SETTINGS_MODULE" not in os.environ:
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module()

app = Celery("seo")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def keep_django_logging(**kwargs: Any) -> None:
    """Логи воркера и beat — через LOGGING Django: stdout, `run_id`, маскировка.

    Пока у сигнала есть получатель, Celery не настраивает логи сам и не
    снимает наши обработчики с корневого логгера (ADR-025).
    """
