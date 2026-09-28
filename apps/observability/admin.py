"""Админка блока наблюдаемости: журнал запусков фоновых задач (E2-01).

Только просмотр: строки пишут базовый класс задач (`config/queue.py`) и
импорт таблицы, человек журнал не правит.
"""

import json
from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.observability.models import TaskRun
from config.admin import NoDeleteAdmin

# Названия задач по-русски. Задачи нет в списке — показывается её имя.
TASK_LABELS = {
    "heartbeat": "Пульс очереди",
    "queue_probe": "Проверка очереди",
    "import_workbook": "Загрузка таблицы",
}
# Сколько символов ошибки видно в списке; целиком — на странице запуска.
ERROR_PREVIEW = 120


def task_label(name: str) -> str:
    return TASK_LABELS.get(name, name)


def duration_text(duration_ms: int | None) -> str | None:
    """850 мс → «0,9 с», 95 000 мс → «1 мин 35 с»."""
    if duration_ms is None:
        return None
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} с".replace(".", ",")
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} мин {rest} с"


class TaskFilter(admin.SimpleListFilter):
    """Задачи — по-русски, как в колонке, а не внутренними именами."""

    title = "задача"
    parameter_name = "task"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        names = TaskRun.objects.order_by().values_list("task_name", flat=True).distinct()
        return sorted(((name, task_label(name)) for name in names), key=lambda item: item[1])

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.value():
            return queryset.filter(task_name=self.value())
        return queryset


@admin.register(TaskRun)
class TaskRunAdmin(NoDeleteAdmin):
    list_display = ("task", "status", "started_at", "duration", "attempt", "error_preview", "run")
    list_filter = ("status", TaskFilter, "started_at")
    # run_id — целиком или первые 8 символов, как в строке лога.
    search_fields = ("run_id", "error")
    ordering = ("-started_at", "-pk")
    list_per_page = 100
    fields = (
        "task",
        "status",
        "started_at",
        "finished_at",
        "duration",
        "attempt",
        "error",
        "run_id",
        "payload_view",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="задача", ordering="task_name")
    def task(self, obj: TaskRun) -> str:
        return task_label(obj.task_name)

    @admin.display(description="длительность", ordering="duration_ms")
    def duration(self, obj: TaskRun) -> str | None:
        return duration_text(obj.duration_ms)

    @admin.display(description="попытка")
    def attempt(self, obj: TaskRun) -> int | None:
        attempt = (obj.payload or {}).get("attempt")
        return int(attempt) if attempt is not None else None

    @admin.display(description="ошибка")
    def error_preview(self, obj: TaskRun) -> str | None:
        if not obj.error or len(obj.error) <= ERROR_PREVIEW:
            return obj.error
        return obj.error[:ERROR_PREVIEW] + "…"

    @admin.display(description="run_id", ordering="run_id")
    def run(self, obj: TaskRun) -> str | None:
        # Первые 8 символов, как в строке лога: [7f3a1c2e].
        return str(obj.run_id)[:8] if obj.run_id else None

    @admin.display(description="параметры")
    def payload_view(self, obj: TaskRun) -> SafeString | None:
        if obj.payload is None:
            return None
        text = json.dumps(obj.payload, ensure_ascii=False, indent=2)
        return format_html("<pre>{}</pre>", text)
