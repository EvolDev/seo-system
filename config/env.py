"""Чтение настроек из переменных окружения.

Пустое значение считается отсутствующим: в `.env.example` все ключи
перечислены с пустыми значениями, и незаполненный ключ должен падать
при старте, а не тихо превращаться в пустую строку.
"""

import os

from django.core.exceptions import ImproperlyConfigured

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _raw(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def env_str(name: str, default: str | None = None) -> str:
    """Строка. Без значения и без default — ошибка конфигурации."""
    value = _raw(name)
    if value is not None:
        return value
    if default is not None:
        return default
    raise ImproperlyConfigured(f"Переменная окружения {name} не задана")


def env_bool(name: str, default: bool) -> bool:
    """true/false, 1/0, yes/no, on/off в любом регистре."""
    value = _raw(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"Переменная окружения {name}: ожидается true/false, получено {value!r}"
    )


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Список через запятую, пробелы и пустые элементы отбрасываются."""
    value = _raw(name)
    if value is None:
        return list(default) if default is not None else []
    return [item.strip() for item in value.split(",") if item.strip()]
