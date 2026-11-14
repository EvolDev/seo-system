"""Настройки окружений: прод не включает то, что опасно в проде."""

import importlib
import logging
from collections.abc import Iterator
from types import ModuleType

import pytest
from django.core.exceptions import ImproperlyConfigured

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
    @pytest.fixture(autouse=True)
    def _site_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # На сервере секретный путь обязателен (ADR-081).
        monkeypatch.setenv("SITE_PATH", "tihaya-gavan")

    def test_site_path_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SITE_PATH")
        with pytest.raises(ImproperlyConfigured, match="SITE_PATH"):
            _load_prod()

    @pytest.mark.parametrize("value", ["/", "admin x", "Maneki", "ab", "../etc"])
    def test_site_path_only_simple_words(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("SITE_PATH", value)
        with pytest.raises(ImproperlyConfigured, match="SITE_PATH"):
            _load_prod()

    def test_everything_under_site_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SITE_PATH", "/tihaya-gavan/")
        prod = _load_prod()
        assert prod.SITE_PATH == "tihaya-gavan"
        assert prod.URL_ROOT == "tihaya-gavan/"
        assert prod.STATIC_URL == "tihaya-gavan/static/"

    def test_debug_off_even_if_env_says_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_DEBUG", "true")
        assert _load_prod().DEBUG is False

    def test_cookies_secure(self) -> None:
        prod = _load_prod()
        assert prod.SESSION_COOKIE_SECURE is True
        assert prod.CSRF_COOKIE_SECURE is True
        # Не «sessionid» локальной версии: куки localhost общие у портов.
        assert prod.SESSION_COOKIE_NAME == "seo_session"

    def test_logs_as_json(self) -> None:
        assert _load_prod().LOGGING["handlers"]["stdout"]["formatter"] == "json"

    def test_behind_caddy(self) -> None:
        """HTTPS снимает Caddy: схема запроса и адрес человека — из его заголовков."""
        prod = _load_prod()
        assert prod.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
        assert prod.BEHIND_PROXY is True
        # Проверка контейнера ходит по 127.0.0.1 — без него контейнер «болен».
        assert {"127.0.0.1", "localhost"} <= set(prod.ALLOWED_HOSTS)

    def test_hsts_off_until_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _load_prod().SECURE_HSTS_SECONDS == 0
        monkeypatch.setenv("SECURE_HSTS_SECONDS", "31536000")
        assert _load_prod().SECURE_HSTS_SECONDS == 31536000

    def test_static_from_image(self) -> None:
        prod = _load_prod()
        assert prod.MIDDLEWARE[:2] == [
            "django.middleware.security.SecurityMiddleware",
            "whitenoise.middleware.WhiteNoiseMiddleware",
        ]
        # Без отпечатка в именах: переходы без перезагрузки узнают скрипты по пути.
        backend = prod.STORAGES["staticfiles"]["BACKEND"]
        assert backend == "whitenoise.storage.CompressedStaticFilesStorage"
        # Локальная версия без WhiteNoise: статику отдаёт сервер разработки.
        assert "whitenoise.middleware.WhiteNoiseMiddleware" not in base.MIDDLEWARE

    def test_database_connection_reused(self) -> None:
        database = _load_prod().DATABASES["default"]
        assert database["CONN_MAX_AGE"] == 60
        assert database["CONN_HEALTH_CHECKS"] is True


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
