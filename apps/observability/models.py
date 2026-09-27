"""Блок 6 модели данных: доменный журнал наблюдаемости (ADR-011).

Проверки, вызовы LLM, запуски задач, расход платных API. Хранятся
вечно, в отличие от технических логов. Как модели повторяют
`schema.sql` — ADR-029.

`run_id` во всех таблицах по умолчанию берётся из текущей цепочки
(`config.run_id.current_run_id`): строка, созданная внутри
`with bind_run_id(...)`, получает его сама. Вне цепочки — пусто.
Это значение по умолчанию на стороне Python, в схеме базы его нет.

Деньги: расход LLM пишется только в `llm_calls`, остальных API — только
в `api_usage`. Иначе себестоимость статьи посчитается дважды.
"""

from typing import ClassVar

from django.db import models

from config.db import PgEnumField, PgNow
from config.run_id import current_run_id


class CheckStatus(models.TextChoices):
    OK = "ok", "В порядке"
    FAILED = "failed", "Не пройдена"
    WARNING = "warning", "Предупреждение"
    ERROR = "error", "Ошибка проверки"


class Performer(models.TextChoices):
    SYSTEM = "system", "Система"
    HUMAN = "human", "Человек"


class LlmStatus(models.TextChoices):
    OK = "ok", "Успешно"
    ERROR = "error", "Ошибка"
    INVALID_JSON = "invalid_json", "Невалидный JSON"
    TIMEOUT = "timeout", "Таймаут"


class TaskStatus(models.TextChoices):
    RUNNING = "running", "Выполняется"
    SUCCESS = "success", "Успешно"
    FAILED = "failed", "Ошибка"


class Check(models.Model):
    """Запись единого журнала проверок: индексация, живость ссылки, серость…

    Полиморфна: `entity_type` + `entity_id` без внешнего ключа — ради
    одного журнала и одного запроса «что проверять сегодня»
    (`v_overdue_checks`, индекс `idx_checks_due`). Новая проверка — новая
    строка, срок следующей берётся из последней.
    """

    entity_type = models.TextField("тип объекта")
    entity_id = models.BigIntegerField("id объекта")
    check_type = models.TextField("тип проверки")
    status = PgEnumField("результат", enum_type="check_status", choices=CheckStatus.choices)
    result = models.JSONField("подробности", null=True, blank=True)
    performed_by = PgEnumField(
        "кто проверил",
        enum_type="performer",
        choices=Performer.choices,
        default=Performer.SYSTEM,
        db_default=Performer.SYSTEM,
    )
    run_id = models.UUIDField("run_id", null=True, blank=True, default=current_run_id)
    checked_at = models.DateTimeField("дата проверки", db_default=PgNow())
    next_check_at = models.DateTimeField("следующая проверка", null=True, blank=True)

    class Meta:
        db_table = "checks"
        verbose_name = "проверка"
        verbose_name_plural = "проверки"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity_type", "entity_id"], name="idx_checks_entity"),
            models.Index(fields=["check_type", "next_check_at"], name="idx_checks_due"),
        ]

    def __str__(self) -> str:
        return f"{self.check_type} · {self.entity_type} {self.entity_id} · {self.status}"


class LlmCall(models.Model):
    """Один вызов LLM. Пишется до обработки ответа, поэтому видны и упавшие.

    Текст промпта не хранится — только ссылка на вариант
    (`prompt_variant`), иначе база распухнет. Ответ — целиком.
    """

    task = models.TextField("задача")
    model = models.TextField("модель")
    prompt_variant = models.ForeignKey(
        "content.PromptVariant",
        models.PROTECT,
        verbose_name="вариант промпта",
        related_name="llm_calls",
        null=True,
        blank=True,
        db_index=False,
    )
    input_tokens = models.IntegerField("токенов на входе", null=True, blank=True)
    output_tokens = models.IntegerField("токенов на выходе", null=True, blank=True)
    cost_cents = models.IntegerField("стоимость, центы", null=True, blank=True)
    currency = models.CharField("валюта", max_length=3, default="USD", db_default="USD")
    duration_ms = models.IntegerField("длительность, мс", null=True, blank=True)
    status = PgEnumField("результат", enum_type="llm_status", choices=LlmStatus.choices)
    response = models.JSONField("ответ", null=True, blank=True)
    entity_type = models.TextField("тип объекта", null=True, blank=True)
    entity_id = models.BigIntegerField("id объекта", null=True, blank=True)
    run_id = models.UUIDField("run_id", null=True, blank=True, default=current_run_id)
    created_at = models.DateTimeField("дата", db_default=PgNow())

    class Meta:
        db_table = "llm_calls"
        verbose_name = "вызов LLM"
        verbose_name_plural = "вызовы LLM"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["task", "-created_at"], name="idx_llm_task"),
            models.Index(fields=["run_id"], name="idx_llm_run"),
        ]

    def __str__(self) -> str:
        return f"{self.task} · {self.model} · {self.status}"


class TaskRun(models.Model):
    """Запуск фоновой задачи: когда, сколько шёл, чем кончился."""

    task_name = models.TextField("задача")
    status = PgEnumField("состояние", enum_type="task_status", choices=TaskStatus.choices)
    started_at = models.DateTimeField("начало", db_default=PgNow())
    finished_at = models.DateTimeField("конец", null=True, blank=True)
    duration_ms = models.IntegerField("длительность, мс", null=True, blank=True)
    error = models.TextField("ошибка", null=True, blank=True)
    payload = models.JSONField("параметры", null=True, blank=True)
    run_id = models.UUIDField("run_id", null=True, blank=True, default=current_run_id)

    class Meta:
        db_table = "task_runs"
        verbose_name = "запуск задачи"
        verbose_name_plural = "запуски задач"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["task_name", "-started_at"], name="idx_taskruns_name"),
        ]

    def __str__(self) -> str:
        return f"{self.task_name} · {self.status}"


class ApiUsage(models.Model):
    """Расход платного API, кроме LLM: Ahrefs, DataForSEO, Voyage."""

    provider = models.TextField("провайдер")
    endpoint = models.TextField("метод", null=True, blank=True)
    units = models.IntegerField("юнитов или запросов", null=True, blank=True)
    cost_cents = models.IntegerField("стоимость, центы", null=True, blank=True)
    currency = models.CharField("валюта", max_length=3, default="USD", db_default="USD")
    run_id = models.UUIDField("run_id", null=True, blank=True, default=current_run_id)
    created_at = models.DateTimeField("дата", db_default=PgNow())

    class Meta:
        db_table = "api_usage"
        verbose_name = "расход API"
        verbose_name_plural = "расход API"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["provider", "-created_at"], name="idx_usage_provider"),
        ]

    def __str__(self) -> str:
        return f"{self.provider} · {self.endpoint or '—'}"
