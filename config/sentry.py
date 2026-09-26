"""Ошибки — в Sentry (ADR-011, ADR-013).

Инициализируется из настроек Django. Пустой `SENTRY_DSN` — Sentry
выключен: так по умолчанию локально и в тестах.

Каждое событие перед отправкой проходит `before_send`: получает тег
`run_id` текущей цепочки и ту же маскировку секретов, что и логи
(`config.logs`) — в событии есть локальные переменные кадров стека и
последние записи лога, и секрет туда попадает легко.
"""

from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.types import Event, Hint

from config.logs import mask_strings, redact
from config.run_id import current_run_id


def before_send(event: Event, hint: Hint) -> Event | None:
    run_id = current_run_id()
    if run_id:
        event.setdefault("tags", {})["run_id"] = str(run_id)
    return cast(Event, mask_strings(redact(event)))


def sentry_options(dsn: str, environment: str) -> dict[str, Any]:
    return {
        "dsn": dsn,
        "environment": environment,
        "integrations": [DjangoIntegration()],
        # Не отправляем IP, куки и пользователя: они не нужны для разбора.
        "send_default_pii": False,
        # Только ошибки, без замеров производительности.
        "traces_sample_rate": 0.0,
        "before_send": before_send,
    }


def init_sentry(dsn: str, environment: str) -> bool:
    """Включает Sentry, если задан DSN. Возвращает, включён ли."""
    if not dsn:
        return False
    sentry_sdk.init(**sentry_options(dsn, environment))
    return True


def disable_sentry() -> None:
    """Выключает Sentry в этом процессе (тесты).

    Пустая строка, а не None: при dsn=None Sentry сам берёт SENTRY_DSN
    из окружения и включается снова.
    """
    sentry_sdk.init(dsn="")
