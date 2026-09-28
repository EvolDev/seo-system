"""Проект Django: настройки, инфраструктура для всех приложений.

Приложение Celery загружается вместе с Django — иначе `shared_task` в
веб-процессе не знал бы, в какую очередь отправлять задачи (E2-01).
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
