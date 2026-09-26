import io
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

import pytest

from config.logs import (
    MASK,
    ConsoleFormatter,
    JsonFormatter,
    RunIdFilter,
    SecretsFilter,
    is_secret_name,
)
from config.run_id import bind_run_id, new_run_id

SECRET = "sk-ant-test-0123456789abcdef"

Emit = Callable[[logging.Logger], None]


def _capture(formatter: logging.Formatter, emit: Emit) -> str:
    """Пропускает записи через обработчик, собранный как в `logging_config()`."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.addFilter(RunIdFilter())
    handler.addFilter(SecretsFilter())
    logger = logging.getLogger("tests.logs")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
    return stream.getvalue()


def _json(emit: Emit) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(_capture(JsonFormatter(), emit))
    return result


def _console(emit: Emit) -> str:
    # Цвет уровня — ANSI-коды; для проверок текста они не нужны.
    return re.sub(r"\033\[[\d;]*m", "", _capture(ConsoleFormatter(), emit))


FORMATTERS = pytest.mark.parametrize("formatter", [JsonFormatter, ConsoleFormatter])


class TestJson:
    def test_base_fields(self) -> None:
        record = _json(lambda log: log.info("привет %s", "мир"))
        assert record["level"] == "INFO"
        assert record["logger"] == "tests.logs"
        assert record["message"] == "привет мир"
        assert record["run_id"] is None
        assert "ts" in record

    def test_run_id_inside_chain(self) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            record = _json(lambda log: log.info("шаг"))
        assert record["run_id"] == str(run_id)

    def test_extra_fields(self) -> None:
        record = _json(lambda log: log.info("аудит", extra={"site": "example.com", "dr": 42}))
        assert record["site"] == "example.com"
        assert record["dr"] == 42

    def test_exception(self) -> None:
        def emit(log: logging.Logger) -> None:
            try:
                raise ValueError("сломалось")
            except ValueError:
                log.exception("упало")

        record = _json(emit)
        assert "ValueError: сломалось" in record["exc"]


class TestConsole:
    def test_line(self) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            line = _console(lambda log: log.info("аудит", extra={"site": "example.com"}))
        assert re.fullmatch(
            rf"\d\d:\d\d:\d\d INFO    tests\.logs \[{str(run_id)[:8]}\] аудит site=example\.com\n",
            line,
        )

    def test_no_brackets_outside_chain(self) -> None:
        assert "[" not in _console(lambda log: log.info("вне цепочки"))

    def test_exception(self) -> None:
        def emit(log: logging.Logger) -> None:
            try:
                raise ValueError("сломалось")
            except ValueError:
                log.exception("упало")

        assert "ValueError: сломалось" in _console(emit)


@FORMATTERS
class TestMasking:
    @pytest.fixture(autouse=True)
    def secret_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    def test_value_in_message(self, formatter: type[logging.Formatter]) -> None:
        out = _capture(formatter(), lambda log: log.info("ключ %s", SECRET))
        assert SECRET not in out
        assert MASK in out

    def test_config_dump(self, formatter: type[logging.Formatter]) -> None:
        # Намеренное логирование конфига — критерий приёмки E0-03.
        out = _capture(formatter(), lambda log: log.info("конфиг: %s", dict(os.environ)))
        assert SECRET not in out

    def test_traceback(self, formatter: type[logging.Formatter]) -> None:
        def emit(log: logging.Logger) -> None:
            try:
                raise RuntimeError(f"bad key {SECRET}")
            except RuntimeError:
                log.exception("упало")

        out = _capture(formatter(), emit)
        assert SECRET not in out
        assert "RuntimeError" in out

    def test_secret_key_in_extra_not_from_env(self, formatter: type[logging.Formatter]) -> None:
        token = "token-from-api-response"
        out = _capture(formatter(), lambda log: log.info("ответ", extra={"access_token": token}))
        assert token not in out

    def test_nested_dict_in_args(self, formatter: type[logging.Formatter]) -> None:
        password = "nested-password-value"
        payload = {"auth": {"user": "u", "password": password}}
        out = _capture(formatter(), lambda log: log.info("запрос %s", payload))
        assert password not in out
        assert "'user': 'u'" in out

    def test_escaped_value(
        self, formatter: type[logging.Formatter], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Кавычка и обратная косая черта экранируются и в repr(), и в JSON.
        tricky = 'pa"ss\\word-123'
        monkeypatch.setenv("POSTGRES_PASSWORD", tricky)
        out = _capture(formatter(), lambda log: log.info("%s", {"raw": tricky}))
        assert "word-123" not in out
        assert MASK in out

    def test_short_value_not_masked(
        self, formatter: type[logging.Formatter], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POSTGRES_PASSWORD", "seo")
        out = _capture(formatter(), lambda log: log.info("seo-system"))
        assert "seo-system" in out


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "POSTGRES_PASSWORD", "SENTRY_DSN", "DATAFORSEO_LOGIN",
     "api_key", "access_token", "csrftoken", "client_secret", "Authorization", "key"],
)  # fmt: skip
def test_secret_names(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["TELEGRAM_CHAT_ID", "keyword", "site", "run_id", "POSTGRES_HOST"])
def test_not_secret_names(name: str) -> None:
    assert not is_secret_name(name)
