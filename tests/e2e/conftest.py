"""Браузерные проверки экранов (E9-09, ADR-046): Chromium без окна, Playwright.

Запуск — `make e2e`: отдельный образ с Chromium (цель `e2e` в Dockerfile),
`make test` эти тесты пропускает (маркер `e2e`). Сервер — `live_server`
pytest-django: настоящий сервер в потоке теста на тестовой базе, со статикой.

Каждый тестовый файл ставит себе
`pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True,
serialized_rollback=True)]`: живому серверу нужна база с настоящими
транзакциями, а `serialized_rollback` возвращает после теста строки, которые
заводят миграции (настройки, продавец Collaborator).
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer


@pytest.fixture(autouse=True, scope="session")
def orm_beside_playwright() -> Iterator[None]:
    """ORM в теле теста рядом с Playwright.

    Синхронный Playwright держит в потоке теста цикл asyncio, а Django,
    увидев цикл, запрещает обращаться к базе («SynchronousOnlyOperation»):
    решил бы, что его зовут из асинхронного кода. Здесь цикл чужой и база
    вызывается обычным синхронным кодом — запрет снимаем на время проверок.
    """
    previous = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    yield
    if previous is None:
        del os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"]
    else:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = previous


@pytest.fixture
def admin_page(page: Page, live_server: LiveServer, admin_user: User) -> Iterator[Page]:
    """Вкладка, уже вошедшая в админку под суперпользователем.

    Вход — готовой сессией в cookie, без формы входа: проверки не о ней.
    Ошибка JavaScript на странице роняет проверку: скрипт, упавший молча,
    — та же поломка, что видимая.
    """
    client = Client()
    client.force_login(admin_user)
    session = client.cookies[settings.SESSION_COOKIE_NAME].value
    page.context.add_cookies(
        [{"name": settings.SESSION_COOKIE_NAME, "value": session, "url": live_server.url}]
    )
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )
    yield page
    assert not errors, errors


# --- Переход без перезагрузки -------------------------------------------------
#
# Признак, что страница не перезагружалась: метка в `window` пережила переход
# (после полной перезагрузки окно новое и метки нет). Конец перехода —
# событие `seo:load` (seo/soft-nav.js): содержимое заменено, скрипты запущены.


def mark(page: Page) -> None:
    page.evaluate("window.__seoSameDocument = true")


def same_document(page: Page) -> bool:
    return bool(page.evaluate("window.__seoSameDocument === true"))


@contextmanager
def soft_load(page: Page, timeout: float = 10_000) -> Iterator[None]:
    """Дождаться конца перехода, который сделает тело `with`.

    Страница перезагрузилась целиком — флажка в новом окне нет, ожидание
    кончается сразу, и проверка падает с понятным сообщением.
    """
    page.evaluate(
        "window.__seoLoaded = false;"
        " document.addEventListener('seo:load', () => { window.__seoLoaded = true }, {once: true})"
    )
    yield
    page.wait_for_function("window.__seoLoaded !== false", timeout=timeout)
    assert page.evaluate("window.__seoLoaded === true"), "страница перезагрузилась целиком"


def pick(page: Page, field: str, label: str) -> None:
    """Выбрать значение в списке с поиском (seo/picker.js): штатный select скрыт."""
    box = page.locator(f".seo-picker:has(select[name={field}])")
    box.locator(".seo-picker-input").click()
    box.locator(".seo-picker-input").fill(label)
    box.locator(".seo-picker-list li", has_text=label).first.click()
