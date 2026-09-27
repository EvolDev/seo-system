"""Поля и выражения Postgres, которых нет в Django (ADR-029).

Инфраструктура для моделей всех приложений, поэтому живёт в `config/`,
как логи и `run_id` (ADR-025).
"""

from typing import Any

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper


class PgEnumField(models.TextField):  # type: ignore[type-arg]
    """Колонка типа-перечисления Postgres (`CREATE TYPE … AS ENUM`).

    Сам тип создаёт миграция (`RunSQL`) раньше таблицы — поле только
    объявляет колонку этим типом. Допустимые значения передаются в
    `choices` (`TextChoices`) и совпадают со списком в типе; расхождение
    ловит тест `tests/test_schema_parity.py`.
    """

    def __init__(self, *args: Any, enum_type: str, **kwargs: Any) -> None:
        self.enum_type = enum_type
        super().__init__(*args, **kwargs)

    def db_type(self, connection: BaseDatabaseWrapper) -> str:
        return self.enum_type

    def deconstruct(self) -> Any:
        # Миграции записывают поле через deconstruct: без enum_type
        # поле не восстановится из миграции.
        name, path, args, kwargs = super().deconstruct()
        kwargs["enum_type"] = self.enum_type
        return name, path, args, kwargs


class PgNow(models.Func):
    """`now()` — время начала транзакции, как `DEFAULT now()` в `schema.sql`.

    Штатный `django.db.models.functions.Now` на Postgres даёт
    `STATEMENT_TIMESTAMP()`: у строк одной транзакции время разное,
    и значение по умолчанию расходится со схемой.
    """

    function = "now"
    template = "%(function)s()"
    output_field = models.DateTimeField()
    allowed_default = True
