"""Настройки окружений: прод не включает то, что опасно в проде."""

import importlib
import logging
from collections.abc import Iterator
from types import ModuleType

import pytest

from config.logs import ConsoleFormatter, RunIdFilter, SecretsFilter
from config.settings import base


@pytest.fixture
def reload_base() -> Iterator[None]:
    # Модуль Python исполняется один раз, дальше берётся из кэша импорта.
    # Чтобы base перечитал подменённое окружение, его перезагружаем,
    # а после теста — ещё раз, уже с настоящим окружением.
    yield
    importlib.reload(base)


def _load_prod() -> ModuleType:
    importlib.reload(base)
    return importlib.reload(importlib.import_module("config.settings.prod"))


@pytest.mark.usefixtures("reload_base")
class TestProd:
    def test_debug_off_even_if_env_says_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_DEBUG", "true")
        assert _load_prod().DEBUG is False

    def test_cookies_secure(self) -> None:
        prod = _load_prod()
        assert prod.SESSION_COOKIE_SECURE is True
        assert prod.CSRF_COOKIE_SECURE is True

    def test_logs_as_json(self) -> None:
        assert _load_prod().LOGGING["handlers"]["stdout"]["formatter"] == "json"


def test_local_logs_readable() -> None:
    from config.settings import local

    assert local.LOGGING["handlers"]["stdout"]["formatter"] == "console"


def test_django_loggers_go_through_our_handler() -> None:
    # pytest-django уже применил LOGGING из config.settings.local.
    # Свои обработчики на корневом логгере держит и pytest — берём не его.
    (handler,) = [
        h for h in logging.getLogger().handlers if not type(h).__module__.startswith("_pytest")
    ]
    assert isinstance(handler.formatter, ConsoleFormatter)
    assert {type(f) for f in handler.filters} == {RunIdFilter, SecretsFilter}
    for name in ("django", "django.request", "django.server"):
        # Своих обработчиков нет — запись уходит в корневой один раз.
        assert logging.getLogger(name).handlers == []
