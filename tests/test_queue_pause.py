"""Пауза задачи: `Postpone` откладывает запуск без траты попытки (E2-02).

Воркер изображаем, как в tests/test_throttle.py: запрос задачи — через
`push_request`, отправка в очередь подменена списком.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from celery import shared_task
from celery.exceptions import Ignore
from django.utils import timezone

from apps.observability.models import TaskRun, TaskStatus
from config.queue import MAX_POSTPONE, RUN_ID_ARG, Postpone, QueueTask

pytestmark = pytest.mark.django_db

REASON = "дневной лимит исчерпан"
# До какого времени ждать; None — задача работает. Чистится перед тестом.
pause_until: list[datetime] = []
calls: list[int] = []


@shared_task(base=QueueTask, name="test_pause.wait")
def wait() -> str:
    calls.append(1)
    if pause_until:
        raise Postpone(pause_until[0], REASON)
    return "готово"


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    pause_until.clear()
    calls.clear()
    yield
    pause_until.clear()
    calls.clear()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Что задача отправила в очередь вместо настоящей отправки."""
    messages: list[dict[str, Any]] = []

    def fake_apply_async(
        self: QueueTask,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> None:
        messages.append({"args": args, "kwargs": kwargs, **options})

    monkeypatch.setattr(QueueTask, "apply_async", fake_apply_async)
    return messages


def _in_worker(task_id: str, run_id: str, retries: int = 0) -> Any:
    """Запуск, как у воркера: запрос с id задачи и run_id в аргументах."""
    wait.push_request(
        id=task_id,
        args=[],
        kwargs={RUN_ID_ARG: run_id},
        retries=retries,
        called_directly=False,
        is_eager=False,
        delivery_info={},
    )
    try:
        return wait(run_id=run_id)
    finally:
        wait.pop_request()


class TestPause:
    def test_long_pause_split_into_chunks(self, sent: list[dict[str, Any]]) -> None:
        # До утра далеко: задача уходит в очередь не дольше чем на MAX_POSTPONE,
        # иначе Redis отдал бы её снова по visibility_timeout.
        until = timezone.now() + timedelta(hours=10)
        pause_until.append(until)
        task_id, run_id = str(uuid4()), str(uuid4())

        with pytest.raises(Ignore):
            _in_worker(task_id, run_id, retries=1)

        (message,) = sent
        assert message["task_id"] == task_id
        assert message["kwargs"] == {RUN_ID_ARG: run_id}
        # Попытка не потрачена: номер тот же.
        assert message["retries"] == 1
        assert message["countdown"] == MAX_POSTPONE
        (run,) = TaskRun.objects.filter(task_name="test_pause.wait")
        assert run.status == TaskStatus.RUNNING
        assert (run.payload or {})["waiting"] == {"until": until.isoformat(), "reason": REASON}
        assert run.error is None

    def test_short_pause_until_its_time(self, sent: list[dict[str, Any]]) -> None:
        pause_until.append(timezone.now() + timedelta(seconds=120))
        with pytest.raises(Ignore):
            _in_worker(str(uuid4()), str(uuid4()))
        (message,) = sent
        assert 110 < message["countdown"] <= 120

    def test_past_time_waits_a_second(self, sent: list[dict[str, Any]]) -> None:
        pause_until.append(timezone.now() - timedelta(minutes=1))
        with pytest.raises(Ignore):
            _in_worker(str(uuid4()), str(uuid4()))
        (message,) = sent
        assert message["countdown"] == 1

    def test_resumed_task_continues_its_row(self, sent: list[dict[str, Any]]) -> None:
        task_id, run_id = str(uuid4()), str(uuid4())
        pause_until.append(timezone.now() + timedelta(hours=1))
        with pytest.raises(Ignore):
            _in_worker(task_id, run_id)

        pause_until.clear()
        assert _in_worker(task_id, run_id) == "готово"

        (run,) = TaskRun.objects.filter(task_name="test_pause.wait")
        assert run.status == TaskStatus.SUCCESS
        assert "waiting" not in (run.payload or {})
        assert (run.payload or {})["attempt"] == 1

    def test_pause_is_not_an_alert(
        self, sent: list[dict[str, Any]], sentry_events: list[dict[str, Any]]
    ) -> None:
        pause_until.append(timezone.now() + timedelta(hours=1))
        with pytest.raises(Ignore):
            _in_worker(str(uuid4()), str(uuid4()))
        assert sentry_events == []

    def test_without_queue_pause_is_an_error(self) -> None:
        # Без очереди (eager, тесты) ждать до утра на месте нельзя: ошибка с
        # причиной, без повторов — пауза не сбой.
        pause_until.append(timezone.now() + timedelta(hours=1))
        result = wait.delay()
        with pytest.raises(Postpone):
            result.get()
        assert len(calls) == 1
        (run,) = TaskRun.objects.filter(task_name="test_pause.wait")
        assert run.status == TaskStatus.FAILED
        assert run.error == f"Postpone: {REASON}"
