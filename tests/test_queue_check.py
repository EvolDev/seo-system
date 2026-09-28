"""Пульс очереди, проба и команда `queue_check` (E2-01).

Задачи идут в режиме eager (tests/conftest.py): команда находит строки
проб в журнале сразу, без воркера.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from apps.observability.models import TaskRun, TaskStatus
from apps.observability.tasks import ProbeError, heartbeat, queue_probe

pytestmark = pytest.mark.django_db


def _check(*args: str) -> str:
    out = StringIO()
    call_command("queue_check", *args, stdout=out)
    return out.getvalue()


def test_heartbeat_leaves_success_row() -> None:
    heartbeat.delay()
    run = TaskRun.objects.get(task_name="heartbeat")
    assert run.status == TaskStatus.SUCCESS
    assert run.run_id is not None


def test_probe_fails_on_every_attempt() -> None:
    with pytest.raises(ProbeError):
        queue_probe.delay(fail=True).get()
    run = TaskRun.objects.get(task_name="queue_probe")
    assert run.status == TaskStatus.FAILED
    assert isinstance(run.payload, dict)
    assert run.payload["attempt"] == 3
    assert run.error == "ProbeError: проверка очереди: намеренная ошибка"


def test_check_reports_success() -> None:
    output = _check()
    run = TaskRun.objects.get(task_name="queue_probe")
    assert f"run_id: {run.run_id} · проб в очереди: 1" in output
    assert "Успешно · попытка 1" in output


def test_check_fail_points_to_sentry() -> None:
    output = _check("--fail")
    assert "Ошибка · попытка 3" in output
    assert "ProbeError: проверка очереди: намеренная ошибка" in output
    assert "ищите по тегу run_id:" in output


def test_check_throttle_spaces_starts() -> None:
    output = _check("--count", "2", "--throttle")
    starts = list(
        TaskRun.objects.filter(task_name="queue_probe")
        .order_by("started_at")
        .values_list("started_at", flat=True)
    )
    assert len(starts) == 2
    # Проба — не чаще раза в секунду на ключ.
    assert (starts[1] - starts[0]).total_seconds() >= 0.9
    assert output.count("Успешно") == 2


def test_check_without_waiting() -> None:
    output = _check("--wait", "0")
    assert "Не ждём" in output


def test_check_hints_when_worker_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Воркер не запущен: пробы ушли в очередь, а в журнале их нет.
    monkeypatch.setattr(queue_probe, "delay", lambda **kwargs: None)
    output = _check("--wait", "0.2")
    assert "закончено проб: 0 из 1" in output
    assert "docker compose ps" in output
