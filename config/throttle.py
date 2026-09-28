"""Ограничение скорости: не чаще одного запуска за интервал на ключ (E2-01, ADR-039).

Задача объявляет, что ограничивать, атрибутом `throttle`:

    @shared_task(
        base=QueueTask,
        name="crawl_page",
        throttle=Throttle("crawler", per_second=1, key=lambda url: domain_of(url)),
    )

Перед запуском базовый класс (`config/queue.py`) занимает ключ в Redis на
время интервала. Ключ занят — задача откладывается на остаток интервала,
попытка не тратится. Так лимит держит очередь, а не код задачи.

Занять ключ — одна команда `SET key 1 NX PX <интервал>`: значение
запишется, только если ключа нет, и само исчезнет через интервал.
Проверка и запись идут одним действием, поэтому два процесса не займут
ключ одновременно.

Подходит, когда на один ключ ждёт немного задач сразу, как домены у
краулера: отложенные задачи просыпаются вместе и снова спорят за ключ.
Для общего лимита API на большие пачки лучше встроенный `rate_limit`
Celery — его выбирает задача клиента API.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, cast

import redis
from django.conf import settings

PREFIX = "throttle"


@dataclass(frozen=True)
class Throttle:
    """`name` — группа ключей («crawler»); `per_second` — запусков в секунду на ключ.

    `key` получает аргументы задачи и возвращает ключ внутри группы
    (домен из URL). None вместо функции — один ключ на всю группу; None
    из функции — эту задачу не ограничиваем.
    """

    name: str
    per_second: float
    key: Callable[..., str | None] | None = None

    def __post_init__(self) -> None:
        if self.per_second <= 0:
            raise ValueError(f"Throttle {self.name!r}: per_second должен быть больше нуля")

    @property
    def interval_ms(self) -> int:
        return max(1, round(1000 / self.per_second))

    def key_for(self, args: Sequence[Any], kwargs: Mapping[str, Any]) -> str | None:
        """Полный ключ для задачи с этими аргументами или None — без ограничения."""
        if self.key is None:
            return self.name
        part = self.key(*args, **kwargs)
        return None if part is None else f"{self.name}:{part}"

    def acquire(self, key: str) -> float:
        """Занимает ключ. 0 — занят нами, можно запускать; иначе — секунд до освобождения."""
        client = _client()
        name = f"{PREFIX}:{key}"
        if client.set(name, 1, nx=True, px=self.interval_ms):
            return 0.0
        # redis-py описывает ответ и для асинхронного клиента; у нас он синхронный.
        ttl_ms = cast(int, client.pttl(name))
        # -2: ключ исчез между командами — пробовать почти сразу.
        return max(ttl_ms, 1) / 1000


@cache
def _client() -> redis.Redis:
    """Клиент Redis на процесс: тот же Redis, что у очереди.

    `cache` — один объект на процесс. После fork воркера redis-py сам
    открывает в дочернем процессе свои соединения.
    """
    return redis.Redis.from_url(settings.CELERY_BROKER_URL)
