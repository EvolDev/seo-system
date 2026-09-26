"""Сквозной идентификатор запуска цепочки (`02-ARCHITECTURE.md`, принцип 5).

`run_id` создаётся в точке входа — запуск аудита, генерации — и
передаётся каждому шагу цепочки. Логи, события Sentry и строки доменных
таблиц одного запуска помечаются одним значением.

Внутри процесса текущий `run_id` хранится в `ContextVar`: это
«глобальная переменная» со своим значением в каждом потоке и каждой
async-задаче, поэтому параллельные цепочки не путаются. Между
процессами (веб → воркер) он передаётся явно, аргументом задачи — это
делает базовый класс задачи Celery (E2-01, ADR-025).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID, uuid4

_current: ContextVar[UUID | None] = ContextVar("run_id", default=None)


def new_run_id() -> UUID:
    """Новый `run_id` для точки входа цепочки."""
    return uuid4()


def current_run_id() -> UUID | None:
    """`run_id` текущей цепочки или None, если код идёт вне цепочки."""
    return _current.get()


@contextmanager
def bind_run_id(run_id: UUID | str) -> Iterator[UUID]:
    """Всё, что выполняется внутри `with`, относится к цепочке `run_id`.

    Строка принимается, потому что аргументы задач Celery приходят из
    JSON. При выходе восстанавливается прежнее значение, так что
    вложенные блоки безопасны.
    """
    value = run_id if isinstance(run_id, UUID) else UUID(run_id)
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
