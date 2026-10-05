"""Поля и выражения Postgres, которых нет в Django (ADR-029).

Инфраструктура для моделей всех приложений, поэтому живёт в `config/`,
как логи и `run_id` (ADR-025).
"""

import logging
from collections.abc import Iterable
from typing import Any

from django.db import connection, models
from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger(__name__)


def analyze(tables: Iterable[str]) -> None:
    """`ANALYZE` названных таблиц: после массовой записи статистика устаревает.

    Планировщик по старой статистике выбирает плохой план, и запрос к
    представлению, который шёл секунду, идёт минуту (05.10.2026: «Площадки»
    после загрузки размещений — 48 секунд). Автоочистка дошла бы и сама, но
    человек открывает экран раньше неё. Имена берутся из журнала загрузки,
    то есть из самой базы, и всё равно экранируются.
    """
    names = sorted(set(tables))
    if not names:
        return
    with connection.cursor() as cursor:
        for name in names:
            cursor.execute(f"ANALYZE {connection.ops.quote_name(name)}")
    logger.info("статистика обновлена", extra={"tables": names})


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


class PgCurrentDate(models.Func):
    """`CURRENT_DATE` — дата начала транзакции, как `DEFAULT current_date` в схеме.

    Дата — по часовому поясу соединения с базой. Django ставит соединению
    UTC, поэтому с 00:00 до 03:00 по Москве это ещё вчерашняя дата.
    """

    template = "CURRENT_DATE"
    output_field = models.DateField()
    allowed_default = True
