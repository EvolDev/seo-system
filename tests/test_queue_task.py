"""Базовый класс задач очереди: журнал запусков, run_id, повторы, алерт (E2-01).

Задачи идут в режиме eager (tests/conftest.py): сразу, в процессе теста,
повторы — без пауз. Паузы проверяются по записям в логе.
"""

import json
import logging
import os
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from celery import shared_task
from celery.exceptions import Ignore
from django.utils import timezone

from apps.observability.models import TaskRun, TaskStatus
from config.queue import FIRST_RETRY_DELAY, MAX_ATTEMPTS, RUN_ID_ARG, QueueTask
from config.run_id import bind_run_id, current_run_id

pytestmark = pytest.mark.django_db

Events = list[dict[str, Any]]

# run_id, который видела задача, — по вызову на попытку. Чистится перед тестом.
seen: list[str] = []


class Flaky(ConnectionError):
    """Сбой внешнего API, который проходит сам."""


@shared_task(base=QueueTask, name="test_queue.double")
def double(value: int, *, api_token: str = "") -> int:
    seen.append(str(current_run_id()))
    return value * 2


@shared_task(base=QueueTask, name="test_queue.flaky")
def flaky(failures: int) -> str:
    """Падает `failures` раз подряд, потом выполняется."""
    seen.append(str(current_run_id()))
    if len(seen) <= failures:
        raise Flaky(f"сбой {len(seen)}")
    return "готово"


@shared_task(base=QueueTask, name="test_queue.chain")
def chain_start(value: int) -> None:
    """Точка входа цепочки: следующую задачу ставит из своего тела."""
    seen.append(str(current_run_id()))
    double.delay(value)


@shared_task(base=QueueTask, name="test_queue.leaky")
def leaky() -> None:
    raise RuntimeError(f"ключ {os.environ['TEST_QUEUE_API_KEY']} не подошёл")


@shared_task(base=QueueTask, name="test_queue.ignored")
def ignored() -> None:
    raise Ignore()


@pytest.fixture(autouse=True)
def clear_seen() -> Iterator[None]:
    seen.clear()
    yield
    seen.clear()


def _run(task_name: str) -> TaskRun:
    (run,) = TaskRun.objects.filter(task_name=task_name)
    return run


def _payload(run: TaskRun) -> dict[str, Any]:
    assert isinstance(run.payload, dict)
    return run.payload


class TestJournal:
    def test_success_is_one_row(self) -> None:
        result = double.delay(21)
        assert result.get() == 42
        run = _run("test_queue.double")
        assert run.status == TaskStatus.SUCCESS
        assert run.error is None
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at
        assert run.duration_ms is not None
        assert run.duration_ms >= 0
        assert run.payload == {"task_id": result.id, "args": [21], "kwargs": {}, "attempt": 1}

    def test_secret_arguments_masked(self) -> None:
        double.delay(1, api_token="t0ken-value")
        assert _payload(_run("test_queue.double"))["kwargs"] == {"api_token": "***"}

    def test_error_text_without_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_QUEUE_API_KEY", "sk-live-1234567890")
        with pytest.raises(RuntimeError):
            leaky.delay().get()
        run = _run("test_queue.leaky")
        assert run.status == TaskStatus.FAILED
        assert run.error == "RuntimeError: ключ *** не подошёл"
        assert "sk-live" not in json.dumps(run.payload, ensure_ascii=False)

    def test_ignore_is_not_an_error(self) -> None:
        ignored.delay()
        run = _run("test_queue.ignored")
        assert run.status == TaskStatus.SUCCESS
        assert run.error is None

    def test_direct_call_is_recorded(self) -> None:
        assert double(3) == 6
        run = _run("test_queue.double")
        assert run.status == TaskStatus.SUCCESS
        assert _payload(run)["task_id"] is None

    def test_interrupted_run_continues_its_row(self) -> None:
        # Воркер убит посреди задачи: строка осталась «Выполняется». Redis
        # отдаёт то же сообщение снова, с тем же id, — строка продолжается.
        task_id = str(uuid4())
        interrupted = TaskRun.objects.create(
            task_name="test_queue.double",
            status=TaskStatus.RUNNING,
            started_at=timezone.now() - timedelta(hours=1),
            payload={"task_id": task_id, "args": [2], "kwargs": {}, "attempt": 1},
        )
        double.apply((2,), task_id=task_id)
        assert _run("test_queue.double").pk == interrupted.pk
        interrupted.refresh_from_db()
        assert interrupted.status == TaskStatus.SUCCESS
        # Длительность — от первого старта: видно, сколько задача ждала.
        assert interrupted.duration_ms is not None
        assert interrupted.duration_ms >= 3600 * 1000

    def test_finished_and_old_rows_not_continued(self) -> None:
        task_id = str(uuid4())
        rows = [
            TaskRun(task_name="test_queue.double", status=TaskStatus.SUCCESS),
            TaskRun(
                task_name="test_queue.double",
                status=TaskStatus.RUNNING,
                started_at=timezone.now() - timedelta(days=3),
            ),
        ]
        for row in rows:
            row.payload = {"task_id": task_id, "attempt": 1}
            row.save()
        double.apply((2,), task_id=task_id)
        assert TaskRun.objects.filter(task_name="test_queue.double").count() == 3

    def test_redelivered_message_marked(self) -> None:
        # Сообщение, которое Redis вернул после остановки воркера.
        double.push_request(
            id=str(uuid4()),
            args=[4],
            kwargs={},
            retries=0,
            called_directly=False,
            delivery_info={"redelivered": True},
        )
        try:
            assert double(4) == 8
        finally:
            double.pop_request()
        assert _payload(_run("test_queue.double"))["redelivered"] is True


class TestRunId:
    def test_task_joins_current_chain(self) -> None:
        with bind_run_id(uuid4()) as run_id:
            double.delay(1)
        assert seen == [str(run_id)]
        assert _run("test_queue.double").run_id == run_id

    def test_outside_chain_each_task_starts_its_own(self) -> None:
        double.delay(1)
        double.delay(2)
        run_ids = set(TaskRun.objects.values_list("run_id", flat=True))
        assert len(run_ids) == 2
        assert None not in run_ids
        assert set(seen) == {str(run_id) for run_id in run_ids}

    def test_next_task_gets_run_id_of_previous(self) -> None:
        chain_start.delay(5)
        first = _run("test_queue.chain")
        second = _run("test_queue.double")
        assert first.run_id is not None
        assert second.run_id == first.run_id
        assert seen == [str(first.run_id)] * 2

    def test_explicit_run_id_wins(self) -> None:
        explicit = uuid4()
        with bind_run_id(uuid4()):
            double.apply_async((1,), {RUN_ID_ARG: str(explicit)})
        assert _run("test_queue.double").run_id == explicit

    def test_retries_keep_run_id(self) -> None:
        # Задача без run_id — как от beat — получает новый, повторы — тот же.
        flaky.delay(2)
        run = _run("test_queue.flaky")
        assert run.run_id is not None
        assert seen == [str(run.run_id)] * 3

    def test_run_id_taken_when_queued_on_commit(
        self, django_capture_on_commit_callbacks: Callable[..., Any]
    ) -> None:
        # Задача уходит после фиксации транзакции, а run_id — того, кто её поставил.
        with django_capture_on_commit_callbacks() as callbacks, bind_run_id(uuid4()) as run_id:
            double.delay_on_commit(3)
        assert not TaskRun.objects.exists()
        for callback in callbacks:
            callback()
        assert _run("test_queue.double").run_id == run_id

    def test_wrong_arguments_fail_when_queued(self) -> None:
        with pytest.raises(TypeError):
            double.delay()
        with pytest.raises(TypeError):
            double.delay(1, unknown=2)
        assert not TaskRun.objects.exists()

    def test_logs_inside_task_carry_run_id(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="config.queue")
        with bind_run_id(uuid4()) as run_id, pytest.raises(Flaky):
            flaky.delay(10).get()
        records = [record for record in caplog.records if record.name == "config.queue"]
        assert records
        assert {record.__dict__["run_id"] for record in records} == {str(run_id)}


class TestRetries:
    def test_failure_then_success(self) -> None:
        assert flaky.delay(1).get() == "готово"
        run = _run("test_queue.flaky")
        assert run.status == TaskStatus.SUCCESS
        assert run.error is None
        assert _payload(run)["attempt"] == 2
        assert _payload(run)["errors"] == [{"attempt": 1, "error": "Flaky: сбой 1"}]

    def test_last_failure_marks_failed(self) -> None:
        with pytest.raises(Flaky):
            flaky.delay(10).get()
        assert len(seen) == MAX_ATTEMPTS == 3
        run = _run("test_queue.flaky")
        assert run.status == TaskStatus.FAILED
        assert run.error == "Flaky: сбой 3"
        assert run.finished_at is not None
        assert _payload(run)["attempt"] == 3
        assert [error["attempt"] for error in _payload(run)["errors"]] == [1, 2]

    def test_pause_doubles(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="config.queue")
        with pytest.raises(Flaky):
            flaky.delay(10).get()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "config.queue" and record.levelno == logging.WARNING
        ]
        assert warnings == [
            f"попытка 1 из 3 не удалась, повтор через {FIRST_RETRY_DELAY} с",
            f"попытка 2 из 3 не удалась, повтор через {FIRST_RETRY_DELAY * 2} с",
        ]


class TestAlert:
    def test_one_event_after_last_attempt(self, sentry_events: Events) -> None:
        with bind_run_id(uuid4()) as run_id, pytest.raises(Flaky):
            flaky.delay(10).get()
        (event,) = sentry_events
        assert event["level"] == "error"
        assert event["logger"] == "config.queue"
        assert event["tags"]["run_id"] == str(run_id)
        assert event["exception"]["values"][-1]["type"] == "Flaky"

    def test_no_event_when_retry_helps(self, sentry_events: Events) -> None:
        flaky.delay(2)
        assert sentry_events == []
