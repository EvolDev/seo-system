"""Задачи очереди блока площадок: курсы валют (E1-07), загрузки файлов (E1-08).

- `exchange_rates_update` — beat раз в сутки, вечером, после публикации
  курсов ЕЦБ: записывает курсы, которых ещё нет. Повторный запуск ничего
  не дублирует: курс валюты на дату один.
- `upload_check` — сводка до записи по загруженному файлу; базу не меняет.
- `upload_write` — запись загрузки в одной транзакции (ADR-044). Повтор уже
  записанную загрузку не трогает.

Каталог Collaborator — 45 000 строк: у задач загрузки свои лимиты времени,
больше общих 10 минут. После последней неудачной попытки загрузка
получает состояние «Ошибка» — иначе экран ждал бы её вечно.
"""

import logging
from typing import Any

from celery import shared_task

from apps.integrations import ecb
from apps.sites.rates import save_rates
from apps.sites.uploads import service
from config.queue import QueueTask

logger = logging.getLogger(__name__)


@shared_task(base=QueueTask, name="exchange_rates_update")
def exchange_rates_update() -> int:
    saved = save_rates(ecb.fetch_rates())
    logger.info("курсы ЕЦБ записаны", extra={"new_rates": saved})
    return saved


class UploadTask(QueueTask):
    """Задача загрузки: после последней попытки — «Ошибка» у загрузки с текстом."""

    soft_time_limit = 30 * 60
    time_limit = 35 * 60

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        # Celery зовёт on_failure один раз — когда попыток больше нет.
        super().on_failure(exc, task_id, args, kwargs, einfo)
        if args:
            service.fail(int(args[0]), f"Не получилось: {exc}. Подробности — в «Запусках задач».")


@shared_task(base=UploadTask, name="upload_check")
def upload_check(upload_id: int) -> None:
    service.check(upload_id)


@shared_task(base=UploadTask, name="upload_write")
def upload_write(
    upload_id: int,
    action: str = "all",
    parts: list[str] | None = None,
    spec: dict[str, Any] | None = None,
) -> None:
    service.write(upload_id, action, parts, spec)
