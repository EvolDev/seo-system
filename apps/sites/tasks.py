"""Задачи очереди блока площадок: курсы валют (E1-07, ADR-043).

- `exchange_rates_update` — beat раз в сутки, вечером, после публикации
  курсов ЕЦБ: записывает курсы, которых ещё нет. Повторный запуск ничего
  не дублирует: курс валюты на дату один.
"""

import logging

from celery import shared_task

from apps.integrations import ecb
from apps.sites.rates import save_rates
from config.queue import QueueTask

logger = logging.getLogger(__name__)


@shared_task(base=QueueTask, name="exchange_rates_update")
def exchange_rates_update() -> int:
    saved = save_rates(ecb.fetch_rates())
    logger.info("курсы ЕЦБ записаны", extra={"new_rates": saved})
    return saved
