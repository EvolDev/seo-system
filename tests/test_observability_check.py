"""Команда `observability_check` — критерии приёмки E0-03 одним запуском."""

import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.core.management import call_command

from config.logs import JsonFormatter, RunIdFilter, SecretsFilter

SECRET = "sk-ant-test-0123456789abcdef"
LOGGER = "apps.observability.management.commands.observability_check"


Records = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> Iterator[Records]:
    """JSON-записи логгера команды, прошедшие те же фильтры, что в `logging_config()`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    handler.addFilter(SecretsFilter())
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logger.removeHandler(handler)


def test_whole_chain_has_one_run_id(records: Records) -> None:
    out = io.StringIO()
    call_command("observability_check", stdout=out)
    lines = records()
    run_ids = {line["run_id"] for line in lines}
    assert len(lines) >= 4
    assert len(run_ids) == 1
    assert None not in run_ids
    assert f"run_id: {run_ids.pop()}" in out.getvalue()


def test_config_dump_masked(records: Records) -> None:
    call_command("observability_check", stdout=io.StringIO())
    text = json.dumps(records(), ensure_ascii=False)
    assert "ANTHROPIC_API_KEY" in text
    assert SECRET not in text
    # Пароль базы маскируется по имени ключа, какой бы длины он ни был.
    assert "'PASSWORD': '***'" in text


def test_raise_sends_event_with_run_id(
    records: Records, sentry_events: list[dict[str, Any]]
) -> None:
    out = io.StringIO()
    call_command("observability_check", "--raise", stdout=out)
    (event,) = sentry_events
    run_id = records()[0]["run_id"]
    assert event["tags"]["run_id"] == run_id
    assert event["exception"]["values"][0]["type"] == "ObservabilityCheckError"
    assert "Событие передано клиенту Sentry" in out.getvalue()


def test_raise_without_sentry_says_so() -> None:
    out = io.StringIO()
    call_command("observability_check", "--raise", stdout=out)
    assert "Sentry выключен" in out.getvalue()
