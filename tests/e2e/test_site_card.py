"""Карточка площадки (E9-13, ADR-058) — seo/site-card.js.

Разделы сворачиваются щелчком по заголовку, выбор помнится у пользователя на
всех карточках и в другом браузере; переход к свёрнутому разделу его
разворачивает; заметка добавляется по Ctrl+Enter, панель остаётся на месте.
Серверную часть проверяет tests/test_site_card.py.
"""

import datetime as dt
import re
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from playwright.sync_api import Browser, Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.sites.models import (
    MetricSource,
    Product,
    Seller,
    Site,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
)
from apps.sites.uploads.plan import start_of_day
from apps.workspace import card

CLOSED = re.compile(r"\bseo-closed\b")

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"
SITES = "/admin/sites/productsitelatest/"


@pytest.fixture
def sites() -> list[Site]:
    """known.com и other.com в рабочем списке Convertio; у known.com — цена с прочими данными."""
    Product.objects.get_or_create(name="Convertio", defaults={"domain": "convertio.co"})
    site_list = SiteList.objects.create(name="Октябрь")
    result = []
    for domain in ("known.com", "other.com"):
        site = Site.objects.create(domain=domain, language="en")
        SiteMetric.objects.create(site=site, dr=40, organic_traffic=1000)
        SiteListItem.objects.create(site_list=site_list, site=site)
        result.append(site)
    known = result[0]
    price = SitePrice.objects.create(
        site=known,
        seller=Seller.collaborator(),
        placement_cents=20000,
        source=MetricSource.CSV_IMPORT,
        checked_at=start_of_day(dt.date(2026, 9, 27)),
        reviewed_at=timezone.now(),
        extra={"Sponsored": "yes"},
    )
    known.price = price
    known.save(update_fields=["price"])
    for number in range(6):
        SiteNote.objects.create(site=known, body=f"Заметка из таблицы {number}", source="таблица")
    return result


def _open(page: Page, domain: str) -> None:
    page.locator("#result_list a[data-panel]", has_text=domain).click()
    expect(page.locator(PANEL).locator(".seo-panel-title")).to_have_text(domain)


def _section(page: Page, key: str):  # type: ignore[no-untyped-def]
    return page.locator(f'{PANEL} [data-section="{key}"]')


@pytest.fixture
def second_browser(browser: Browser, live_server: LiveServer, admin_user: User) -> Iterator[Page]:
    """Тот же пользователь в другом браузере: своя сессия, своё хранилище."""
    client = Client()
    client.force_login(admin_user)
    session = client.cookies[settings.SESSION_COOKIE_NAME].value
    context = browser.new_context()
    context.add_cookies(
        [{"name": settings.SESSION_COOKIE_NAME, "value": session, "url": live_server.url}]
    )
    yield context.new_page()
    context.close()


def test_collapse_remembered_on_all_cards(
    admin_page: Page,
    live_server: LiveServer,
    admin_user: User,
    sites: list[Site],
    second_browser: Page,
) -> None:
    """Критерий: свёрнутое помнится у пользователя на всех карточках и в другом браузере."""
    page = admin_page
    page.set_viewport_size({"width": 1920, "height": 1000})
    page.goto(f"{live_server.url}{SITES}")
    _open(page, "known.com")
    data = _section(page, "data")
    expect(data).not_to_have_class(CLOSED)
    expect(data.locator(".seo-card-sec-summary")).to_be_hidden()

    # Ширина — около 860 px, не во весь экран.
    width = page.locator(PANEL).bounding_box()["width"]  # type: ignore[index]
    assert 780 <= width <= 900, width

    with page.expect_response(lambda response: "card-sections" in response.url):
        data.locator(".seo-card-toggle").click()
    expect(data).to_have_class(CLOSED)
    expect(data.locator(".seo-card-sec-summary")).to_have_text("Collaborator · 1 поле")
    expect(data.locator(".seo-card-toggle")).to_have_attribute("aria-expanded", "false")
    assert card.closed_sections(admin_user) == {"data"}

    # Другая площадка — тот же раздел свёрнут, остальные развёрнуты.
    page.keyboard.press("Escape")
    _open(page, "other.com")
    expect(_section(page, "data")).to_have_class(CLOSED)
    expect(_section(page, "notes")).not_to_have_class(CLOSED)

    # Другой браузер — отдельная страница карточки, свёрнуто так же.
    other = second_browser
    other.goto(f"{live_server.url}/admin/sites/site/{sites[0].pk}/card/")
    expect(other.locator('[data-section="data"]')).to_have_class(CLOSED)

    # «Свернуть все» и обратно.
    with page.expect_response(lambda response: "card-sections" in response.url):
        page.locator(f"{PANEL} [data-card-all]").click()
    expect(page.locator(f"{PANEL} .seo-card-sec.seo-closed")).to_have_count(4)
    expect(page.locator(f"{PANEL} [data-card-all]")).to_have_text("Развернуть все")
    with page.expect_response(lambda response: "card-sections" in response.url):
        page.locator(f"{PANEL} [data-card-all]").click()
    expect(page.locator(f"{PANEL} .seo-card-sec.seo-closed")).to_have_count(0)
    assert card.closed_sections(admin_user) == frozenset()


def test_jump_opens_closed_section(
    admin_page: Page, live_server: LiveServer, admin_user: User, sites: list[Site]
) -> None:
    """Критерий: переход к свёрнутому разделу разворачивает его и показывает под шапкой."""
    card.set_closed(admin_user, ("price", "notes"), closed=True)
    page = admin_page
    page.set_viewport_size({"width": 1440, "height": 800})
    page.goto(f"{live_server.url}{SITES}")
    _open(page, "known.com")
    notes = _section(page, "notes")
    expect(notes).to_have_class(CLOSED)
    expect(notes.locator(".seo-card-sec-summary")).to_contain_text("последняя — Система")

    with page.expect_response(lambda response: "card-sections" in response.url):
        page.locator(f"{PANEL} [data-jump='notes']").click()
    expect(notes).not_to_have_class(CLOSED)
    expect(notes.locator(".seo-note-form textarea")).to_be_visible()
    expect(page.locator(f"{PANEL} [data-jump='notes']")).to_have_class(re.compile(r"\bseo-on\b"))
    # Раздел — сразу под закреплённой шапкой, а не спрятан под ней.
    page.wait_for_function(
        """() => {
          const head = document.querySelector('.seo-panel .seo-card-head').getBoundingClientRect();
          const sec = document.querySelector('.seo-panel [data-section="notes"]');
          const top = sec.getBoundingClientRect().top;
          return top >= head.bottom - 1 && top <= head.bottom + 40;
        }"""
    )
    assert card.closed_sections(admin_user) == {"price"}


def test_note_with_ctrl_enter(
    admin_page: Page, live_server: LiveServer, admin_user: User, sites: list[Site]
) -> None:
    """Критерий: заметка по Ctrl+Enter — сверху ленты с ником и временем; панель на месте."""
    page = admin_page
    page.set_viewport_size({"width": 1440, "height": 700})
    page.goto(f"{live_server.url}{SITES}")
    _open(page, "known.com")
    page.locator(f"{PANEL} [data-jump='notes']").click()
    body = page.locator(f"{PANEL} .seo-panel-body")
    page.wait_for_function("document.querySelector('.seo-panel .seo-panel-body').scrollTop > 0")
    page.wait_for_timeout(600)  # плавная прокрутка закончилась
    before = body.evaluate("node => node.scrollTop")

    area = page.locator(f"{PANEL} .seo-note-form textarea")
    area.fill("Позвонить продавцу")
    area.press("Control+Enter")
    first = page.locator(f"{PANEL} .seo-note-feed li").first
    expect(first).to_contain_text("Позвонить продавцу")
    expect(first.locator(".seo-note-who")).to_contain_text("admin")
    expect(first.locator(".seo-note-who")).to_contain_text("Convertio")
    expect(first.locator("time")).to_contain_text("сегодня, ")
    expect(first).not_to_have_class(re.compile(r"seo-note-system"))
    # Системные — приглушённые, «Система».
    expect(page.locator(f"{PANEL} .seo-note-system").first).to_contain_text("Система")

    after = body.evaluate("node => node.scrollTop")
    assert abs(after - before) < 5, (before, after)
    note = SiteNote.objects.get(site=sites[0], author=admin_user)
    assert (note.body, note.product.name if note.product else None) == (
        "Позвонить продавцу",
        "Convertio",
    )
