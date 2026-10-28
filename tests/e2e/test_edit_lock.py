"""Блокировка правки в браузере (ADR-077): seo/edit-lock.js и панель записи.

Размещения площадки правит другой человек — панель открывает их только для
просмотра, в углу окошко «кто и с какого времени правит»; освободились — панель
перечитывает запись, можно править. Первая правка берёт блокировку, закрытие
панели её снимает. Запись поверх чужой блокировки не проходит: сообщение и
запись заново. Серверную часть проверяет tests/test_edit_locks.py.
"""

import time
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site
from apps.workspace import locks
from apps.workspace.models import EditLock

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"
ALERT = "[data-seo-lock-alert]"


@pytest.fixture
def convertio() -> Product:
    return Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})[0]


@pytest.fixture
def placement(convertio: Product) -> Placement:
    return Placement.objects.create(
        site=Site.objects.create(domain="dci.com.br"),
        product=convertio,
        status=PlacementStatus.IN_WORK,
    )


@pytest.fixture
def anna(convertio: Product) -> User:
    return User.objects.create_superuser("anna", "anna@example.com", "x", first_name="Анна")


def _key(placement: Placement) -> str:
    return locks.placements_key(placement.site_id, placement.product_id)


def _until(check: Callable[[], bool], seconds: float = 5) -> bool:
    """Запрос из браузера уходит в фоне — ждём, пока база это покажет."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return check()


def _open(page: Page, live_server: LiveServer) -> None:
    page.goto(f"{live_server.url}/admin/placements/placement/")
    page.locator("#result_list a", has_text="dci.com.br").first.click()
    expect(page.locator(PANEL)).to_be_visible()


def test_locked_by_other_then_free(
    admin_page: Page, live_server: LiveServer, placement: Placement, anna: User
) -> None:
    """Правит Анна — просмотр и окошко; Анна закрыла — запись заново, можно править."""
    locks.take(anna, _key(placement))
    page = admin_page
    _open(page, live_server)
    alert = page.locator(ALERT)
    expect(alert).to_contain_text("Размещения Convertio на этой площадке правит Анна с")
    expect(alert).to_contain_text("пока только просмотр")
    expect(page.locator(f"{PANEL} [data-panel-save]")).to_have_count(0)

    locks.release(anna, _key(placement))
    # Опрос раз в 20 секунд — в тесте зовём его сразу.
    page.evaluate("window.seoLock.poll()")
    expect(alert).to_have_count(0)
    expect(page.locator(".seo-toast", has_text="Размещения свободны")).to_be_visible()
    expect(page.locator(f"{PANEL} [data-panel-save]")).to_be_visible()


def test_first_edit_takes_and_close_releases(
    admin_page: Page, live_server: LiveServer, placement: Placement, admin_user: User
) -> None:
    page = admin_page
    _open(page, live_server)
    key = _key(placement)
    assert not EditLock.objects.filter(key=key).exists()
    page.locator(f"{PANEL} textarea[name=comment]").fill("Договорились на 120 €")
    assert _until(lambda: EditLock.objects.filter(key=key, user=admin_user).exists())
    page.locator(f"{PANEL} [data-panel-cancel]").click()
    expect(page.locator(PANEL)).to_be_hidden()
    assert _until(lambda: not EditLock.objects.filter(key=key).exists())


def test_save_over_others_lock_is_refused(
    admin_page: Page, live_server: LiveServer, placement: Placement, anna: User
) -> None:
    """Панель открыли, пока было свободно; Анна взяла раньше записи — отказ и просмотр."""
    page = admin_page
    _open(page, live_server)
    locks.take(anna, _key(placement))
    page.locator(f"{PANEL} [data-panel-save]").click()
    expect(page.locator(".seo-toast", has_text="изменения не записаны")).to_be_visible()
    expect(page.locator(ALERT)).to_contain_text("правит Анна с")
    expect(page.locator(f"{PANEL} [data-panel-save]")).to_have_count(0)
    placement.refresh_from_db()
    assert placement.status == PlacementStatus.IN_WORK
