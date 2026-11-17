"""«Плагины» в настоящем браузере (ADR-084): скачанный с сайта архив работает.

Chromium загружает расширение из архива, который отдала сама система, — так же,
как человек после «Скачать .zip»: manifest с адресом системы, папка
`seo-gray-extension`. Страница «Плагины» видит версию, которую сообщает расширение.
"""

import io
import json
import os
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from playwright.sync_api import BrowserContext, Page, Playwright, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites import plugins

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PAGE = reverse("admin:plugins")
Launch = Callable[[str | None], BrowserContext]


@pytest.fixture
def launch(
    playwright: Playwright, tmp_path: Path, live_server: LiveServer, admin_user: User
) -> Iterator[Launch]:
    """Chromium с расширением из скачанного архива; версию можно подменить."""
    client = Client()
    client.force_login(admin_user)
    contexts: list[BrowserContext] = []

    def start(version: str | None = None) -> BrowserContext:
        host = live_server.url.split("://", 1)[1]
        response = client.get(reverse("admin:plugin_download", args=["gray"]), HTTP_HOST=host)
        assert response.status_code == 200
        unpacked = tmp_path / f"unpacked-{len(contexts)}"
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            bundle.extractall(unpacked)
        folder = unpacked / plugins.GRAY.archive_folder
        if version is not None:
            manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
            manifest["version"] = version
            (folder / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / f"profile-{len(contexts)}"),
            channel="chromium",
            headless=True,
            args=[f"--disable-extensions-except={folder}", f"--load-extension={folder}"],
            env={**os.environ, "HOME": str(tmp_path)},
        )
        session = client.cookies[settings.SESSION_COOKIE_NAME].value
        context.add_cookies(
            [{"name": settings.SESSION_COOKIE_NAME, "value": session, "url": live_server.url}]
        )
        contexts.append(context)
        return context

    yield start
    for context in contexts:
        context.close()


def test_downloaded_extension_is_seen(launch: Launch, live_server: LiveServer) -> None:
    page = launch(None).new_page()
    page.goto(live_server.url + PAGE)
    status = page.locator("[data-plugin-status]")
    expect(status).to_have_text(f"У вас стоит версия {plugins.GRAY.version} — актуальная.")
    expect(page.locator(".seo-plugin")).to_have_class("seo-plugin is-current")


def test_older_extension_asks_to_update(launch: Launch, live_server: LiveServer) -> None:
    page = launch("1.1.9").new_page()
    page.goto(live_server.url + PAGE)
    expect(page.locator("[data-plugin-status]")).to_contain_text(
        f"У вас стоит версия 1.1.9, здесь — {plugins.GRAY.version}. Обновите"
    )


def test_without_extension(admin_page: Page, live_server: LiveServer) -> None:
    page = admin_page
    page.goto(live_server.url + PAGE)
    expect(page.locator("[data-plugin-status]")).to_contain_text(
        "В этом браузере расширения не видно", timeout=6000
    )
    # Пункт меню — последним в «Работе», кнопка отдаёт архив.
    expect(page.locator("#nav-sidebar .app-work a").last).to_have_text("Плагины")
    with page.expect_download() as download:
        page.locator(".seo-plugin-download").click()
    assert download.value.suggested_filename == plugins.GRAY.filename
