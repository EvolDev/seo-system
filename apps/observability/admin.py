"""Админка блока наблюдаемости: запуски фоновых задач (E2-01), расход API (E2-02).

Только просмотр: запуски пишут базовый класс задач (`config/queue.py`) и
импорт таблицы, расход — клиенты внешних API. Человек журналы не правит.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.integrations.serp import spent_today
from apps.observability.models import ApiUsage, TaskRun, TaskStatus
from config.admin import RecordAdmin

# Названия задач по-русски. Задачи нет в списке — показывается её имя.
TASK_LABELS = {
    "heartbeat": "Пульс очереди",
    "queue_probe": "Проверка очереди",
    "import_workbook": "Загрузка таблицы",
}
# Сколько символов ошибки видно в списке; целиком — на странице запуска.
ERROR_PREVIEW = 120


# Сервисы по-человечески. Нет в списке — показывается как записан.
PROVIDER_LABELS = {
    "serper": "Serper · выдача Google",
    "ahrefs": "Ahrefs",
    "voyage": "Voyage · эмбеддинги",
}


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


def money_text(cents: Decimal | int | None, currency: str = "USD") -> str | None:
    """Центы с долями → «$0,001», «$5,00»: не меньше двух знаков, без лишних нулей."""
    if cents is None:
        return None
    amount = f"{(Decimal(cents) / 100).normalize():f}"
    whole, _, fraction = amount.partition(".")
    fraction = fraction.ljust(2, "0")
    number = f"{whole},{fraction}"
    return f"${number}" if currency == "USD" else f"{number} {currency}"


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
class TaskRunAdmin(RecordAdmin):
    list_display = (
        "task",
        "status",
        "started_at",
        "duration",
        "attempt",
        "waiting",
        "error_preview",
        "run",
    )
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

    @admin.display(description="пауза")
    def waiting(self, obj: TaskRun) -> str | None:
        """Задача ждёт (config/queue.py, Postpone): до какого времени и почему."""
        waiting = (obj.payload or {}).get("waiting")
        if obj.status != TaskStatus.RUNNING or not waiting:
            return None
        until = timezone.localtime(datetime.fromisoformat(waiting["until"]))
        return f"до {until:%d.%m %H:%M}: {waiting['reason']}"

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


@admin.register(ApiUsage)
class ApiUsageAdmin(RecordAdmin):
    """Расход платных API, кроме LLM: строка — один платный запрос."""

    list_display = ("created_at", "provider_name", "endpoint", "units", "cost", "run")
    list_filter = ("provider", "created_at")
    search_fields = ("run_id",)
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-pk")
    list_per_page = 100
    fields = ("created_at", "provider_name", "endpoint", "units", "cost", "run_id")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        # Над списком — главное: сколько ушло на выдачу сегодня и где лимит.
        budget = money_text(settings.SERP_DAILY_BUDGET_CENTS)
        subtitle = f"Выдача Google сегодня: {money_text(spent_today())} из {budget} в сутки"
        return super().changelist_view(request, {**(extra_context or {}), "subtitle": subtitle})

    @admin.display(description="сервис", ordering="provider")
    def provider_name(self, obj: ApiUsage) -> str:
        return PROVIDER_LABELS.get(obj.provider, obj.provider)

    @admin.display(description="стоимость", ordering="cost_cents")
    def cost(self, obj: ApiUsage) -> str | None:
        return money_text(obj.cost_cents, obj.currency)

    @admin.display(description="run_id", ordering="run_id")
    def run(self, obj: ApiUsage) -> str | None:
        return str(obj.run_id)[:8] if obj.run_id else None
