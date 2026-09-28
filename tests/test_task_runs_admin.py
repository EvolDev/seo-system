"""Экран «Запуски задач»: только просмотр, фильтры, поиск по run_id (E2-01)."""

from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse

from apps.observability.admin import ERROR_PREVIEW, duration_text, task_label
from apps.observability.models import TaskRun, TaskStatus

pytestmark = pytest.mark.django_db

LONG_ERROR = "ProbeError: " + "очень длинное сообщение " * 20


@pytest.fixture
def runs() -> list[TaskRun]:
    return [
        TaskRun.objects.create(
            task_name="heartbeat",
            status=TaskStatus.SUCCESS,
            duration_ms=1300,
            payload={"task_id": "a", "attempt": 1},
            run_id=uuid4(),
        ),
        TaskRun.objects.create(
            task_name="queue_probe",
            status=TaskStatus.FAILED,
            duration_ms=95_000,
            error=LONG_ERROR,
            payload={"task_id": "b", "attempt": 3, "errors": [{"attempt": 1, "error": "сбой"}]},
            run_id=uuid4(),
        ),
    ]


def _list(client: Client, **params: str) -> list[TaskRun]:
    response = client.get(reverse("admin:observability_taskrun_changelist"), params)
    assert response.status_code == 200
    return list(response.context["cl"].result_list)


def test_list_in_russian(admin_client: Client, runs: list[TaskRun]) -> None:
    response = admin_client.get(reverse("admin:observability_taskrun_changelist"))
    content = response.content.decode()
    for text in ("Запуски задач", "Пульс очереди", "Проверка очереди", "1,3 с", "1 мин 35 с"):
        assert text in content
    # В списке — начало ошибки, целиком — на странице запуска.
    assert LONG_ERROR[:ERROR_PREVIEW] + "…" in content
    assert LONG_ERROR not in content
    assert str(runs[1].run_id)[:8] in content


def test_newest_first(admin_client: Client, runs: list[TaskRun]) -> None:
    assert _list(admin_client) == [runs[1], runs[0]]


def test_filter_by_status(admin_client: Client, runs: list[TaskRun]) -> None:
    assert _list(admin_client, status__exact=TaskStatus.FAILED) == [runs[1]]


def test_filter_by_task(admin_client: Client, runs: list[TaskRun]) -> None:
    assert _list(admin_client, task="heartbeat") == [runs[0]]
    response = admin_client.get(reverse("admin:observability_taskrun_changelist"))
    (task_filter,) = [f for f in response.context["cl"].filter_specs if f.title == "задача"]
    assert dict(task_filter.lookup_choices) == {
        "heartbeat": "Пульс очереди",
        "queue_probe": "Проверка очереди",
    }


def test_search_by_short_run_id(admin_client: Client, runs: list[TaskRun]) -> None:
    # Первые 8 символов — как в строке лога: [7f3a1c2e].
    assert _list(admin_client, q=str(runs[1].run_id)[:8]) == [runs[1]]


def test_run_page_read_only(admin_client: Client, runs: list[TaskRun]) -> None:
    url = reverse("admin:observability_taskrun_change", args=[runs[1].pk])
    response = admin_client.get(url)
    assert response.status_code == 200
    assert response.context["has_change_permission"] is False
    content = response.content.decode()
    assert LONG_ERROR in content
    assert "&quot;errors&quot;" in content
    assert admin_client.post(url, {}).status_code == 403


def test_no_add_no_delete(admin_client: Client, runs: list[TaskRun]) -> None:
    assert admin_client.get(reverse("admin:observability_taskrun_add")).status_code == 403
    delete = reverse("admin:observability_taskrun_delete", args=[runs[0].pk])
    assert admin_client.get(delete).status_code == 403


def test_duration_text() -> None:
    assert duration_text(None) is None
    assert duration_text(40) == "0,0 с"
    assert duration_text(1300) == "1,3 с"
    assert duration_text(95_000) == "1 мин 35 с"


def test_task_label_falls_back_to_name() -> None:
    assert task_label("import_workbook") == "Загрузка таблицы"
    assert task_label("new_task") == "new_task"
