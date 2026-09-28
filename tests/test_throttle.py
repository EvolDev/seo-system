"""Ограничение скорости: ключ в Redis, перенос задачи без траты попытки (E2-01).

Ключи — в настоящем Redis из compose, у каждого теста свои: имя группы со
случайной частью. Сами исчезают через интервал.
"""

import time
from typing import Any
from uuid import uuid4

import pytest
from celery import shared_task
from celery.exceptions import Ignore

from apps.observability.models import TaskRun
from config.queue import RUN_ID_ARG, QueueTask
from config.throttle import Throttle

pytestmark = pytest.mark.django_db

# Запусков в секунду у тестовой задачи: интервал 50 мс, тесты почти не ждут.
FAST = 20


@shared_task(
    base=QueueTask,
    name="test_throttle.fetch",
    throttle=Throttle(f"test-{uuid4()}", per_second=FAST, key=lambda domain: domain),
)
def fetch(domain: str) -> str:
    return domain


def _group() -> str:
    return f"test-{uuid4()}"


class TestThrottle:
    def test_interval_from_rate(self) -> None:
        assert Throttle("x", per_second=1).interval_ms == 1000
        assert Throttle("x", per_second=4).interval_ms == 250
        assert Throttle("x", per_second=0.5).interval_ms == 2000

    def test_rate_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="больше нуля"):
            Throttle("x", per_second=0)

    def test_key_from_task_arguments(self) -> None:
        assert Throttle("crawler", 1).key_for((), {}) == "crawler"
        by_domain = Throttle("crawler", 1, key=lambda url: url.split("/")[2])
        assert by_domain.key_for(("https://example.com/a",), {}) == "crawler:example.com"
        optional = Throttle("probe", 1, key=lambda **kwargs: kwargs.get("key"))
        assert optional.key_for((), {}) is None
        assert optional.key_for((), {"key": "k"}) == "probe:k"

    def test_second_start_waits_other_key_does_not(self) -> None:
        throttle = Throttle(_group(), per_second=1)
        first, other = f"{throttle.name}:a", f"{throttle.name}:b"
        assert throttle.acquire(first) == 0
        assert 0 < throttle.acquire(first) <= 1
        assert throttle.acquire(other) == 0

    def test_key_frees_after_interval(self) -> None:
        throttle = Throttle(_group(), per_second=FAST)
        key = f"{throttle.name}:a"
        assert throttle.acquire(key) == 0
        time.sleep(2 / FAST)
        assert throttle.acquire(key) == 0


class TestTaskThrottle:
    def test_without_queue_waits_in_place(self) -> None:
        domain = f"{uuid4()}.example"
        started = time.monotonic()
        fetch.delay(domain)
        fetch.delay(domain)
        assert time.monotonic() - started >= 0.8 / FAST
        assert TaskRun.objects.filter(task_name="test_throttle.fetch").count() == 2

    def test_busy_key_postpones_task_in_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Воркер: ключ занят — та же задача уходит в очередь позже, попытка
        # не тратится, строки в журнале нет.
        domain = f"{uuid4()}.example"
        throttle = fetch.throttle
        assert throttle is not None
        key = throttle.key_for((domain,), {})
        assert key is not None
        throttle.acquire(key)

        sent: list[dict[str, Any]] = []

        def fake_apply_async(
            self: QueueTask,
            args: tuple[Any, ...] | None = None,
            kwargs: dict[str, Any] | None = None,
            **options: Any,
        ) -> None:
            sent.append({"args": args, "kwargs": kwargs, **options})

        monkeypatch.setattr(QueueTask, "apply_async", fake_apply_async)
        task_id, run_id = str(uuid4()), str(uuid4())
        # Запрос, как у воркера: вторая попытка, run_id в аргументах сообщения.
        fetch.push_request(
            id=task_id,
            args=[domain],
            kwargs={RUN_ID_ARG: run_id},
            retries=1,
            called_directly=False,
            is_eager=False,
            delivery_info={},
        )
        try:
            with pytest.raises(Ignore):
                fetch(domain, run_id=run_id)
        finally:
            fetch.pop_request()

        (message,) = sent
        assert message["task_id"] == task_id
        assert message["args"] == (domain,)
        assert message["kwargs"] == {RUN_ID_ARG: run_id}
        assert message["retries"] == 1
        assert 0 < message["countdown"] <= 1 / FAST
        assert not TaskRun.objects.exists()
