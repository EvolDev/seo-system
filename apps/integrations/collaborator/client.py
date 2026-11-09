"""Клиент официального API Collaborator — только чтение (E11-01, ADR-079).

    deals = CollaboratorClient.from_env().deals(on_page=lambda page, pages: ...)

API кабинета рекламодателя: `https://collaborator.pro/api/public/…`, ключ —
личный токен из профиля в заголовке `X-Api-Key` (`COLLABORATOR_API_KEY`). Все
методы API только читают; берём один — сделки рекламодателя `deal/list`, по 20
на страницу, все страницы до `pagination.pageCount`. Параметры `per-page`,
`projects[]` и `language` сервер не принимает — отвечает 302 на страницу
входа (проба 08.11.2026), поэтому их нет.

Ключ не попадает ни в ошибки, ни в логи: в адресе его нет, заголовки и тело
ответа в текст ошибки не пишутся, а значения секретных переменных окружения
логгер маскирует сам (`config.logs`).
"""

from collections.abc import Callable
from typing import Any

import httpx

from config.env import env_str

BASE_URL = "https://collaborator.pro/api/public"
DEALS_PATH = "/deal/list"
KEY_NAME = "COLLABORATOR_API_KEY"
TIMEOUT_SECONDS = 30
# Предохранитель от ответа, у которого страницы не кончаются: 186 сделок на
# 08.11.2026 — 10 страниц.
MAX_PAGES = 500

# Ход: (готовая страница, всего страниц).
Progress = Callable[[int, int], None]


class CollaboratorError(Exception):
    """Collaborator недоступен или ответил не тем — текст для человека, без ключа."""


class NoApiKey(CollaboratorError):
    """Ключа API нет в окружении."""

    def __init__(self) -> None:
        super().__init__(
            f"Нет ключа API Collaborator: впишите {KEY_NAME} в .env и перезапустите"
            " приложение и очередь."
        )


def has_key() -> bool:
    return bool(env_str(KEY_NAME, default=""))


class CollaboratorClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        # transport подменяют тесты: запросы уходят в функцию, а не в сеть.
        self._transport = transport

    @classmethod
    def from_env(cls, transport: httpx.BaseTransport | None = None) -> "CollaboratorClient":
        key = env_str(KEY_NAME, default="")
        if not key:
            raise NoApiKey()
        return cls(key, transport)

    def deals(self, on_page: Progress | None = None) -> list[dict[str, Any]]:
        """Все сделки рекламодателя — как их отдаёт API, страница за страницей."""
        items: list[dict[str, Any]] = []
        page, pages = 1, 1
        # `with` закрывает соединение по выходу из блока, даже при ошибке.
        with httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            transport=self._transport,
            headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
            # Переадресация — это страница входа, а не данные: не идём по ней.
            follow_redirects=False,
        ) as client:
            while page <= pages:
                body = _get(client, DEALS_PATH, {"page": page})
                batch, pages = _deals_page(body, page)
                items.extend(batch)
                if on_page is not None:
                    on_page(page, pages)
                page += 1
        return items


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError as error:
        raise CollaboratorError(
            f"Collaborator недоступен: {type(error).__name__}. Попробуйте позже."
        ) from None
    status = response.status_code
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise CollaboratorError(
            f"Collaborator не принял ключ ({status}): проверьте {KEY_NAME} — его могли"
            " выпустить заново кнопкой «Regenerate token»."
        )
    if response.is_redirect:
        raise CollaboratorError(
            f"Collaborator отправил на страницу входа ({status}): ключ не подошёл"
            " или запрос ему не понравился."
        )
    if status != httpx.codes.OK:
        raise CollaboratorError(f"Collaborator ответил {status}. Попробуйте позже.")
    try:
        body = response.json()
    except ValueError:
        raise CollaboratorError("Collaborator ответил не JSON.") from None
    if not isinstance(body, dict):
        raise CollaboratorError("Collaborator ответил не объектом JSON.")
    return body


def _deals_page(body: dict[str, Any], page: int) -> tuple[list[dict[str, Any]], int]:
    """Сделки страницы и сколько всего страниц; ответ не той формы — ошибка."""
    items = body.get("items")
    pagination = body.get("pagination")
    if not isinstance(items, list) or not isinstance(pagination, dict):
        raise CollaboratorError("В ответе Collaborator нет списка сделок (items, pagination).")
    pages = pagination.get("pageCount")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
        raise CollaboratorError(f"Collaborator прислал странное число страниц: {pages!r}.")
    if pages > MAX_PAGES:
        raise CollaboratorError(
            f"Collaborator обещает {pages} страниц сделок — больше {MAX_PAGES}."
        )
    if not all(isinstance(item, dict) for item in items):
        raise CollaboratorError(f"На странице {page} сделки не объектами JSON.")
    return items, pages
