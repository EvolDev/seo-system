"""Кто и откуда меняет статус — отметка для истории статусов (ADR-049).

Строку истории пишет триггер Postgres при каждой смене статуса площадки у
продукта и размещения: так в историю попадает любая смена, даже в обход
`save()`. Кто и откуда — знает только код, поэтому перед записью он кладёт
отметку в переменные транзакции (`set_config(…, true)` — значение живёт до
конца транзакции), а триггер её читает. Не отметил — в истории пусто.

Два шага, как у `run_id` (`config/run_id.py`):

- `bind_change(source, actor_id)` — точка входа говорит, откуда работа:
  запрос админки (это делает `ChangeContextMiddleware`), импорт таблицы,
  загрузка. Значение лежит в `ContextVar` — своё у каждого потока.
- `stamped()` — код, который пишет статус, переносит отметку в транзакцию
  на время своей записи и потом возвращает прежнюю. Вызывать внутри
  `transaction.atomic()`: вне транзакции `set_config(…, true)` забудется
  сразу после своего же запроса.

`source` — значение перечисления `status_source` (`StatusSource` в
`apps/sites/models.py`); чужое значение база не примет.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.db import connection
from django.http import HttpRequest, HttpResponse

from config.run_id import current_run_id


@dataclass(frozen=True)
class Change:
    source: str | None = None
    actor_id: int | None = None


# Отметки нет — None, а не Change(): значение по умолчанию у ContextVar общее
# для всех, ruff не пускает туда объект (B039).
_current: ContextVar[Change | None] = ContextVar("change", default=None)

# Переменные транзакции, которые читает триггер (schema.sql, «Функции»).
_KEYS = ("seo.change_source", "seo.change_actor", "seo.change_placement", "seo.change_run_id")
_EMPTY = ("", "", "", "")
# Что сейчас лежит в переменных транзакции: вложенный `stamped()` на выходе
# возвращает отметку внешнего.
_applied: ContextVar[tuple[str, ...]] = ContextVar("change_applied", default=_EMPTY)

# Миграция, которая переводит статусы, отмечает себя этой строкой в RunSQL.
MIGRATION_STAMP_SQL = "SELECT set_config('seo.change_source', 'migration', true)"


def current_change() -> Change:
    return _current.get() or Change()


@contextmanager
def bind_change(source: str | None, actor_id: int | None = None) -> Iterator[Change]:
    """Всё внутри `with` меняет статусы «оттуда» и «от него»."""
    change = Change(source, actor_id)
    token = _current.set(change)
    try:
        yield change
    finally:
        _current.reset(token)


@contextmanager
def stamped(*, source: str | None = None, placement_id: int | None = None) -> Iterator[None]:
    """Отметка для триггера на время записи статуса.

    `source` — вместо того, что дала точка входа: система по размещению
    отмечает себя сама, а человека, который сменил размещение, берёт из
    `bind_change`. `placement_id` — размещение, по которому сменился статус
    площадки.
    """
    change = current_change()
    run_id = current_run_id()
    values = (
        source or change.source or "",
        "" if change.actor_id is None else str(change.actor_id),
        "" if placement_id is None else str(placement_id),
        "" if run_id is None else str(run_id),
    )
    outer = _applied.get()
    _apply(values)
    token = _applied.set(values)
    try:
        yield
    finally:
        _applied.reset(token)
    # Только после удачной записи: после ошибки транзакция прервана, запрос не
    # пройдёт, а откат и так вернёт прежние значения.
    _apply(outer)


def _apply(values: tuple[str, ...]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT " + ", ".join("set_config(%s, %s, true)" for _ in _KEYS),
            [item for pair in zip(_KEYS, values, strict=True) for item in pair],
        )


class ChangeContextMiddleware:
    """Запрос админки: меняет вошедший пользователь, из панели или полной формы.

    Панель (E9-11) просит страницу с заголовком `X-Seo-Partial`. Экраны,
    у которых свой источник (загрузка), переопределяют его своим
    `bind_change`.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return self.get_response(request)
        # Импорт здесь, а не наверху: config.admin тянет админку Django, а этот
        # модуль нужен и моделям, которые грузятся раньше неё.
        from config.admin import is_partial

        with bind_change("panel" if is_partial(request) else "form", int(user.pk)):
            return self.get_response(request)
