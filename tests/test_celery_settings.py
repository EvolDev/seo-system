"""Настройки очереди: доставка без потерь, расписание, логи (E2-01).

Критерий приёмки «при остановке воркера задачи не теряются» держится на
этих настройках; живьём он проверяется остановкой воркера в compose.
"""

import logging
import os

from celery.schedules import crontab
from celery.signals import setup_logging
from django.conf import settings

from config.celery import app as celery_app
from config.queue import MAX_RETRY_DELAY


def test_broker_is_redis_from_env() -> None:
    # В тестах брокер подменён на память (conftest); здесь — сама настройка.
    assert os.environ["REDIS_URL"] == settings.CELERY_BROKER_URL


def test_tasks_survive_worker_stop() -> None:
    conf = celery_app.conf
    # Сообщение удаляется из Redis только после завершения задачи.
    assert conf.task_acks_late is True
    # Задача, роняющая процесс воркера, не возвращается в очередь по кругу.
    assert conf.task_reject_on_worker_lost is False
    assert conf.worker_prefetch_multiplier == 1


def test_visibility_timeout_longer_than_any_wait() -> None:
    # Иначе Redis отдаст ждущее повтора или ещё идущее сообщение второй раз.
    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert timeout > MAX_RETRY_DELAY
    assert timeout > celery_app.conf.task_time_limit > celery_app.conf.task_soft_time_limit


def test_results_live_in_database() -> None:
    assert celery_app.conf.task_ignore_result is True


def test_eager_does_not_swallow_retries() -> None:
    # С True Celery в режиме eager выбрасывает наружу сигнал повтора.
    assert settings.CELERY_TASK_EAGER_PROPAGATES is False


def test_heartbeat_every_hour() -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["heartbeat"]
    assert entry["task"] == "heartbeat"
    assert entry["schedule"] == crontab(minute=0)
    # Пропущенный пульс выбрасывается раньше, чем придёт следующий.
    options = entry["options"]
    assert isinstance(options, dict)
    assert options["expires"] < 60 * 60


def test_scheduled_tasks_are_registered() -> None:
    # Как в воркере: автопоиск tasks.py в приложениях. Ловит опечатку в расписании.
    celery_app.loader.import_default_modules()
    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        assert entry["task"] in celery_app.tasks, name


def test_schedule_in_project_time_zone() -> None:
    assert str(celery_app.conf.timezone) == settings.TIME_ZONE


def test_celery_keeps_django_logging() -> None:
    # Есть получатель setup_logging — Celery не настраивает логи сам и не
    # снимает наш обработчик с корневого логгера.
    receivers = setup_logging.send(
        sender=None, loglevel=logging.INFO, logfile=None, format="", colorize=False
    )
    assert receivers
    assert celery_app.conf.worker_hijack_root_logger is False
    assert celery_app.conf.worker_redirect_stdouts is False
