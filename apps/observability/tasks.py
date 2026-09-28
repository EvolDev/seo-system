"""Задачи очереди блока наблюдаемости (E2-01).

`heartbeat` — пульс очереди: beat ставит его раз в час
(`CELERY_BEAT_SCHEDULE`). Строка в «Запусках задач» показывает, что beat
и воркер живы, даже когда других задач нет.

`queue_probe` — проба для `manage.py queue_check`: удачная, падающая на
каждой попытке, долгая или с общим ключом ограничения скорости.
"""

import logging
import time

from celery import shared_task

from config.queue import QueueTask
from config.throttle import Throttle

logger = logging.getLogger(__name__)

# Паузы перед повторами пробы короче обычных — проверка идёт секунды, не минуты.
PROBE_RETRY_DELAY = 2
# Пробы с одним ключом стартуют не чаще раза в секунду; без ключа — без ограничения.
PROBE_THROTTLE = Throttle(
    "queue_probe", per_second=1, key=lambda *args, **kwargs: kwargs.get("throttle_key")
)


class ProbeError(RuntimeError):
    """Намеренная ошибка пробы: по имени её легко найти в журнале и в Sentry."""


@shared_task(base=QueueTask, name="heartbeat")
def heartbeat() -> None:
    logger.info("пульс очереди")


@shared_task(
    base=QueueTask,
    name="queue_probe",
    retry_backoff=PROBE_RETRY_DELAY,
    throttle=PROBE_THROTTLE,
)
def queue_probe(
    *, fail: bool = False, sleep_seconds: float = 0, throttle_key: str | None = None
) -> None:
    if sleep_seconds:
        logger.info("проба идёт %g с", sleep_seconds)
        time.sleep(sleep_seconds)
    if fail:
        raise ProbeError("проверка очереди: намеренная ошибка")
    logger.info("проба выполнена")
