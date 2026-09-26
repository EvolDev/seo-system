"""Технические логи: формат, `run_id`, маскировка секретов (ADR-011, ADR-025).

Логи идут в stdout. Локально — читаемая строка (`ConsoleFormatter`),
в проде — JSON по строке на запись (`JsonFormatter`), чтобы искать
`grep` и `jq`. Формат выбирает модуль настроек через `logging_config()`.

Секреты маскируются двумя способами:
- по значению — значения переменных окружения с секретными именами
  заменяются на `***` в готовой строке, включая traceback;
- по имени — в словарях из аргументов и `extra` значения под ключами
  вроде `api_key`, `password` заменяются на `***`, даже если такого
  значения нет в окружении (токен, пришедший от API).
"""

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Literal

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonFormatter as _BaseJsonFormatter

from config.run_id import current_run_id

# Имена переменных окружения с секретами (`docs/13-CONFIG.md` §1).
SECRET_SUFFIXES = ("_KEY", "_PASSWORD", "_TOKEN", "_DSN", "_LOGIN")
# Ключи словарей, которые считаются секретом целиком или по вхождению.
_SECRET_NAMES = frozenset({"KEY", "APIKEY", "DSN", "LOGIN", "AUTHORIZATION", "COOKIE"})
_SECRET_PARTS = ("SECRET", "PASSWORD", "PASSWD", "TOKEN")

MASK = "***"
# Значения короче не маскируются по значению: иначе локальный пароль
# `seo` превратил бы в `***` каждое упоминание `seo-system`.
MIN_SECRET_LENGTH = 8

# Поля, которые добавляем сами, — не считаются пользовательским `extra`.
_OWN_ATTRS = frozenset({"run_id"})
# runserver кладёт время запроса в `extra`, а в строке оно уже есть.
_CONSOLE_HIDDEN = frozenset({"server_time"})


def is_secret_name(name: str) -> bool:
    upper = name.upper().replace("-", "_")
    return (
        upper in _SECRET_NAMES
        or upper.endswith(SECRET_SUFFIXES)
        or any(part in upper for part in _SECRET_PARTS)
    )


def _secret_values() -> list[str]:
    """Значения секретов из окружения во всех видах, в каких они попадают в строку.

    Читается при каждой записи: окружение меняется в тестах, а переменных
    несколько десятков — это дёшево.
    """
    raw = {
        value.strip()
        for name, value in os.environ.items()
        if is_secret_name(name) and len(value.strip()) >= MIN_SECRET_LENGTH
    }
    variants: set[str] = set()
    for value in raw:
        # Как есть, экранированным для JSON и для repr() — в словаре,
        # выведенном через %s, строки идут в виде repr.
        variants |= {value, json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]}
    # Длинные первыми: если один секрет содержит другой, маскируется целиком.
    return sorted(variants, key=len, reverse=True)


def mask_secrets(text: str) -> str:
    for value in _secret_values():
        text = text.replace(value, MASK)
    return text


def redact(value: Any) -> Any:
    """Копия структуры, где значения под секретными ключами заменены на `***`."""
    if isinstance(value, Mapping):
        return {
            key: MASK if isinstance(key, str) and is_secret_name(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if type(value) is tuple:
        return tuple(redact(item) for item in value)
    return value


def _extra_attrs(record: logging.LogRecord) -> dict[str, Any]:
    return {
        name: value
        for name, value in vars(record).items()
        if name not in RESERVED_ATTRS and name not in _OWN_ATTRS
    }


class RunIdFilter(logging.Filter):
    """Добавляет в запись `run_id` текущей цепочки (None вне цепочки)."""

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = current_run_id()
        record.__dict__["run_id"] = str(run_id) if run_id else None
        return True


class SecretsFilter(logging.Filter):
    """Маскирует секреты по имени ключа в сообщении, аргументах и `extra`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, Mapping):
            record.msg = redact(record.msg)
        if record.args:
            record.args = redact(record.args)
        for name, value in _extra_attrs(record).items():
            record.__dict__[name] = MASK if is_secret_name(name) else redact(value)
        return True


class JsonFormatter(_BaseJsonFormatter):
    """Прод: одна JSON-строка на запись."""

    def __init__(self) -> None:
        super().__init__(
            "{levelname}{name}{message}{run_id}",
            style="{",
            timestamp="ts",
            rename_fields={"levelname": "level", "name": "logger", "exc_info": "exc"},
            json_ensure_ascii=False,
        )

    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        # Маскируем строки до сериализации: после неё секрет с кавычкой или
        # обратной косой чертой уже экранирован и в тексте не находится.
        masked: dict[str, Any] = mask_strings(log_data)
        return masked

    def format(self, record: logging.LogRecord) -> str:
        # Второй проход — для объектов, которые энкодер выводит через str().
        return mask_secrets(super().format(record))


def mask_strings(value: Any) -> Any:
    """Копия структуры, где в каждой строке замаскированы секреты из окружения."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, Mapping):
        return {key: mask_strings(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [mask_strings(item) for item in value]
    return value


_RESET = "\033[0m"
_LEVEL_COLORS = {
    logging.DEBUG: "\033[2m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}


class ConsoleFormatter(logging.Formatter):
    """Локально: `14:02:11 INFO    apps.sites.audit [7f3a1c2e] сообщение key=value`.

    В квадратных скобках — первые 8 символов `run_id`, как короткий хеш
    коммита в git; полное значение — в JSON-формате.
    """

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, "")
        parts = [
            self.formatTime(record, self.datefmt),
            f"{color}{record.levelname:<7}{_RESET}",
            record.name,
        ]
        run_id = record.__dict__.get("run_id")
        if run_id:
            parts.append(f"[{run_id[:8]}]")
        parts.append(record.getMessage())
        parts += [
            f"{name}={value}"
            for name, value in _extra_attrs(record).items()
            if name not in _CONSOLE_HIDDEN and isinstance(value, str | int | float | bool)
        ]
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return mask_secrets(line)


def logging_config(formatter: Literal["json", "console"]) -> dict[str, Any]:
    """Значение `LOGGING` для настроек Django: всё в stdout через один обработчик.

    Фильтры висят на обработчике, а не на логгерах: фильтр логгера не
    видит записи дочерних логгеров, а через обработчик проходят все.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "run_id": {"()": "config.logs.RunIdFilter"},
            "secrets": {"()": "config.logs.SecretsFilter"},
        },
        "formatters": {
            "json": {"()": "config.logs.JsonFormatter"},
            "console": {"()": "config.logs.ConsoleFormatter"},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": formatter,
                "filters": ["run_id", "secrets"],
            },
        },
        "root": {"level": "INFO", "handlers": ["stdout"]},
        # У этих логгеров Django по умолчанию свои обработчики; пустой
        # список их снимает, записи уходят в корневой — без дублей.
        "loggers": {
            "django": {"handlers": [], "level": "INFO", "propagate": True},
            "django.server": {"handlers": [], "level": "INFO", "propagate": True},
        },
    }
