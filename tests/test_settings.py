"""Настройки окружений: прод не включает то, что опасно в проде."""

import importlib
from collections.abc import Iterator
from types import ModuleType

import pytest

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
