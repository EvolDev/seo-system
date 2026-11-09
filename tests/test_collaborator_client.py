"""Клиент API Collaborator: страницы, ошибки, ключ не виден (E11-01).

В сеть тесты не ходят: у клиента подменён транспорт httpx.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from apps.integrations.collaborator import CollaboratorClient, CollaboratorError, NoApiKey, has_key
from apps.integrations.collaborator.client import BASE_URL, DEALS_PATH, KEY_NAME

KEY = "test-collaborator-key-0123456789abcdef"


def _deal(deal_id: int) -> dict[str, Any]:
    return {"id": deal_id, "status": "Posted", "creator": {"id": 1, "name": "example.com"}}


def _pages(total: int, size: int = 2) -> Callable[[httpx.Request], httpx.Response]:
    """Ответы `deal/list` той же формы, что у настоящего API, — по `size` сделок."""
    count = max(1, -(-total // size))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params.get("page", "1"))
        first = (page - 1) * size + 1
        items = [_deal(n) for n in range(first, min(first + size, total + 1))]
        pagination = {"totalCount": total, "pageSize": size, "page": page, "pageCount": count}
        return httpx.Response(200, json={"items": items, "pagination": pagination})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CollaboratorClient:
    return CollaboratorClient(KEY, transport=httpx.MockTransport(handler))


def test_reads_every_page_with_key_header() -> None:
    handler = _pages(total=5)
    progress: list[tuple[int, int]] = []

    deals = _client(handler).deals(on_page=lambda page, pages: progress.append((page, pages)))

    assert [deal["id"] for deal in deals] == [1, 2, 3, 4, 5]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    requests: list[httpx.Request] = handler.seen  # type: ignore[attr-defined]
    assert [str(r.url) for r in requests] == [
        f"{BASE_URL}{DEALS_PATH}?page={page}" for page in (1, 2, 3)
    ]
    assert all(r.headers["X-Api-Key"] == KEY for r in requests)
    assert all(KEY not in str(r.url) for r in requests)


def test_no_deals_is_one_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "pagination": {"pageCount": 0}})

    assert _client(handler).deals() == []


@pytest.mark.parametrize(
    ("status", "headers", "text"),
    [
        (401, {}, "не принял ключ"),
        (403, {}, "не принял ключ"),
        (302, {"Location": "https://collaborator.pro/login"}, "страницу входа"),
        (500, {}, "ответил 500"),
    ],
)
def test_http_errors_are_plain_and_keyless(
    status: int, headers: dict[str, str], text: str, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Даже если бы сервер вернул ключ в теле — в текст ошибки тело не идёт.
        return httpx.Response(status, headers=headers, text=f"echo {KEY}")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(CollaboratorError) as raised:
        _client(handler).deals()

    assert text in str(raised.value)
    assert KEY not in str(raised.value)
    assert KEY not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert KEY not in caplog.text


def test_network_error_is_keyless() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        detail = f"cannot connect, headers {dict(request.headers)}"
        raise httpx.ConnectError(detail, request=request)

    with pytest.raises(CollaboratorError) as raised:
        _client(handler).deals()

    assert "недоступен" in str(raised.value)
    assert "ConnectError" in str(raised.value)
    assert KEY not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[1, 2]",
        '{"items": []}',
        '{"items": [], "pagination": {"pageCount": "10"}}',
        '{"items": [1], "pagination": {"pageCount": 1}}',
        '{"items": [], "pagination": {"pageCount": 100000}}',
    ],
)
def test_strange_answers(body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with pytest.raises(CollaboratorError):
        _client(handler).deals()


def test_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_NAME, raising=False)

    assert has_key() is False
    with pytest.raises(NoApiKey) as raised:
        CollaboratorClient.from_env()
    assert KEY_NAME in str(raised.value)


def test_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_NAME, f"  {KEY} ")
    handler = _pages(total=1)

    assert has_key() is True
    CollaboratorClient.from_env(transport=httpx.MockTransport(handler)).deals()

    requests: list[httpx.Request] = handler.seen  # type: ignore[attr-defined]
    assert requests[0].headers["X-Api-Key"] == KEY
