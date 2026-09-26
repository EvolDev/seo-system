"""Проверка наблюдаемости: `run_id` в логах, маскировка секретов, Sentry (E0-03).

    python manage.py observability_check           # цепочка и дамп конфига в лог
    python manage.py observability_check --raise   # плюс исключение в Sentry

Пригодится и после деплоя: убедиться, что на сервере логи и Sentry
работают так же, как локально.
"""

import logging
import os
from typing import Any

import sentry_sdk
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from config.run_id import bind_run_id, new_run_id

logger = logging.getLogger(__name__)


class ObservabilityCheckError(RuntimeError):
    """Намеренное исключение: по нему событие легко найти в Sentry."""


class Command(BaseCommand):
    help = "Пишет в лог тестовую цепочку с run_id и конфиг; с --raise — ошибку в Sentry."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--raise",
            action="store_true",
            dest="raise_error",
            help="бросить тестовое исключение и отправить его в Sentry",
        )

    def handle(self, *args: Any, raise_error: bool, **options: Any) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            logger.info("цепочка запущена", extra={"step": "start"})
            self._step("предфильтр")
            self._step("оценка")
            # Намеренное логирование конфига: секреты должны выйти как ***.
            logger.info("окружение: %s", dict(os.environ))
            logger.info("база: %s", settings.DATABASES["default"])
            if raise_error:
                self._fail()
            logger.info("цепочка завершена", extra={"step": "finish"})

        self.stdout.write(f"run_id: {run_id}")
        self.stdout.write(
            f"Строки этой цепочки помечены [{str(run_id)[:8]}], в JSON — полным run_id; "
            "собрать их — grep по любому из двух."
        )
        if raise_error:
            if sentry_sdk.get_client().transport is None:
                self.stdout.write("Sentry выключен: SENTRY_DSN пуст, событие никуда не ушло.")
            else:
                sentry_sdk.flush()
                # flush() не сообщает, дошло ли событие: ошибки доставки
                # видны только как предупреждения urllib3 выше.
                self.stdout.write(
                    f"Событие передано клиенту Sentry, ищи по тегу run_id:{run_id}. "
                    "Нет в проекте — смотри предупреждения urllib3 выше."
                )

    def _step(self, name: str) -> None:
        logger.info("шаг выполнен", extra={"step": name})

    def _fail(self) -> None:
        try:
            raise ObservabilityCheckError("проверка Sentry: намеренное исключение")
        except ObservabilityCheckError:
            # logger.exception → запись в лог и событие в Sentry (уровень ERROR),
            # пока run_id ещё привязан.
            logger.exception("тестовая ошибка")
