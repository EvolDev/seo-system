from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.sentry import disable_sentry, sentry_options

# Адрес не резолвится: даже если транспорт не подменится, в сеть ничего не уйдёт.
FAKE_DSN = "https://public@sentry.invalid/1"

Events = list[dict[str, Any]]


@pytest.fixture(autouse=True, scope="session")
def no_real_sentry() -> Iterator[None]:
    """Тесты не шлют события в настоящий Sentry, даже если в `.env` задан SENTRY_DSN.

    Настройки уже вызвали `init_sentry()`; повторный `init` с пустым DSN
    заменяет клиент на такой, у которого нет транспорта.
    """
    disable_sentry()
    yield


class _CaptureTransport(Transport):
    """Складывает события в список вместо отправки."""

    def __init__(self, events: Events) -> None:
        super().__init__()
        self.events = events

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


@pytest.fixture
def sentry_events() -> Iterator[Events]:
    """Sentry включён с настройками проекта, события попадают в этот список."""
    events: Events = []
    sentry_sdk.init(**sentry_options(FAKE_DSN, "test"), transport=_CaptureTransport(events))
    yield events
    disable_sentry()
