import logging
from typing import Any

import pytest
import sentry_sdk

from config.logs import MASK
from config.run_id import bind_run_id, new_run_id
from config.sentry import disable_sentry, init_sentry

SECRET = "sk-ant-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)


def _fail(api_key: str) -> None:
    # api_key — локальная переменная кадра: Sentry прикладывает их к событию.
    raise RuntimeError(f"запрос упал, ключ {api_key}")


def _capture_failure() -> None:
    try:
        _fail(SECRET)
    except RuntimeError as error:
        sentry_sdk.capture_exception(error)


def test_empty_dsn_disables() -> None:
    assert init_sentry("", "local") is False
    assert sentry_sdk.get_client().transport is None


def test_tests_ignore_dsn_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://public@sentry.invalid/1")
    disable_sentry()
    assert sentry_sdk.get_client().transport is None


def test_exception_with_context(sentry_events: list[dict[str, Any]]) -> None:
    run_id = new_run_id()
    with bind_run_id(run_id):
        _capture_failure()
    (event,) = sentry_events
    assert event["tags"]["run_id"] == str(run_id)
    assert event["environment"] == "test"
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "RuntimeError"
    frames = [frame["function"] for frame in exception["stacktrace"]["frames"]]
    assert frames[-1] == "_fail"


def test_no_run_id_tag_outside_chain(sentry_events: list[dict[str, Any]]) -> None:
    _capture_failure()
    (event,) = sentry_events
    assert "run_id" not in event.get("tags", {})


def test_secrets_masked(sentry_events: list[dict[str, Any]]) -> None:
    _capture_failure()
    (event,) = sentry_events
    assert SECRET not in str(event)
    (exception,) = event["exception"]["values"]
    assert MASK in exception["value"]
    assert exception["stacktrace"]["frames"][-1]["vars"]["api_key"] == MASK


def test_error_log_becomes_event(sentry_events: list[dict[str, Any]]) -> None:
    run_id = new_run_id()
    with bind_run_id(run_id):
        logging.getLogger("tests.sentry").error("задача упала, ключ %s", SECRET)
    (event,) = sentry_events
    assert event["tags"]["run_id"] == str(run_id)
    assert SECRET not in str(event)
