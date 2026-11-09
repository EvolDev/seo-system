"""Сверка с Collaborator в браузере (E11-01, ADR-080).

«Обновить из Collaborator» над «Размещениями» — окно с итогом и «Открыть
сверку», без перезагрузки. Экран сверки: «Заполнить отмеченные» — сообщение и
экран перечитан. Панель размещения: «Заполнить из Collaborator» ставит номер,
«Заплачено» и адрес, «Размещено» → «Сохранить» — дата публикации сегодняшняя.
«Поставить «Отказ»» открывает панель с выбранным «Отказ».

API не вызывается: клиент подменён, задачи очереди в тестах выполняются сразу.
"""

import re
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.placements.collaborator import deals
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Seller, Site

from .conftest import mark, same_document

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]

PANEL = ".seo-panel"
KEY = "test-collaborator-key-0123456789abcdef"
CARD = "https://collaborator.pro/creator/article/view?id={}"
ARTICLE = "https://www.contabeis.com.br/noticias/80646/como-transformar-pdf-em-word/"


class FakeClient:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def deals(self, on_page: Any = None) -> list[dict[str, Any]]:
        return self.items


def item(**fields: Any) -> dict[str, Any]:
    """Сделка contabeis.com.br той же формы, что у API, на 08.11."""
    value: dict[str, Any] = {
        "id": 2834957,
        "project": {"id": 144728, "name": "Convertio"},
        "creator": {"id": 549884, "name": "contabeis.com.br", "url": CARD.format(549884)},
        "status": "Pending approval",
        "anchors": [
            {
                "anchor": "transformar pdf em doc com o Convertio",
                "url": "https://convertio.co/pt/pdf-doc/",
            },
            {"anchor": "pdf em docx", "url": "https://convertio.co/pt/pdf-docx/"},
        ],
        "created_at": "2026-10-26 22:46:25",
        "urlOfPublishedArticle": ARTICLE,
        "price": "571.64 EUR",
        "pricePublication": "543.53 EUR",
        "priceSpelling": "28.11 EUR",
        "priceAnnouncement": None,
        "priceInsurance": None,
    }
    value.update(fields)
    return value


@pytest.fixture
def placement() -> Placement:
    convertio, _ = Product.objects.get_or_create(
        name="Convertio", defaults={"domain": "convertio.co"}
    )
    site = Site.objects.create(domain="contabeis.com.br", collaborator_url=CARD.format(549884))
    placement = Placement.objects.create(
        site=site,
        product=convertio,
        seller=Seller.collaborator(),
        status=PlacementStatus.ORDERED,
    )
    for index, link in enumerate(item()["anchors"], start=1):
        PlacementLink.objects.create(
            placement=placement, anchor=link["anchor"], target_url=link["url"], link_index=index
        )
    return placement


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Сделки, которые «отдаёт» Collaborator: список можно менять в тесте."""
    items = [item()]
    monkeypatch.setenv("COLLABORATOR_API_KEY", KEY)
    monkeypatch.setattr(
        "apps.placements.tasks.CollaboratorClient.from_env",
        classmethod(lambda cls: FakeClient(items)),
    )
    return items


def test_refresh_window(
    admin_page: Page, live_server: LiveServer, placement: Placement, api: list[dict[str, Any]]
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/")
    mark(page)

    page.locator("[data-collab-refresh]").click()

    toast = page.locator(".seo-toasts .seo-toast", has_text="Сделки забраны")
    expect(toast).to_contain_text("1 сделка: 1 готова к закрытию, 1 можно заполнить")
    expect(toast.locator("a", has_text="Открыть сверку")).to_be_visible()
    assert same_document(page)
    assert KEY not in page.content()


def test_fill_selected(
    admin_page: Page, live_server: LiveServer, placement: Placement, api: list[dict[str, Any]]
) -> None:
    deals.refresh(FakeClient(api))  # type: ignore[arg-type]
    page = admin_page
    page.goto(f"{live_server.url}/admin/collaborator/#fill")
    mark(page)
    group = page.locator("#collab-fill")
    expect(group).to_be_visible()
    expect(page.locator("#collab-ready")).to_be_hidden()

    group.locator("input[name=deal]").first.check()
    group.locator("[data-collab-fill]").click()

    expect(page.locator(".seo-toast-success")).to_contain_text("Записано в размещения: 1")
    expect(page.locator("#collab-fill .seo-collab-empty")).to_have_text("Заполнять нечего.")
    expect(page.locator("[data-collab-tab='journal'] b")).to_have_text("1")
    assert same_document(page)
    placement.refresh_from_db()
    assert placement.collaborator_order_id == "2834957"
    # «Заплачено» у заявки — только при закрытии в панели.
    assert placement.price_paid_cents is None


def test_panel_fill_then_close(
    admin_page: Page, live_server: LiveServer, placement: Placement, api: list[dict[str, Any]]
) -> None:
    deals.refresh(FakeClient(api))  # type: ignore[arg-type]
    page = admin_page
    page.goto(f"{live_server.url}/admin/collaborator/#ready")
    mark(page)

    page.locator("#collab-ready a[data-panel]", has_text="contabeis.com.br").click()
    panel = page.locator(PANEL)
    block = panel.locator("[data-collab-block]")
    expect(block).to_contain_text("Заявка 2834957")
    expect(block).to_contain_text("€571.64 = €543.53 размещение + €28.11 написание")
    expect(block.locator(".seo-collab-links li.is-ok")).to_have_count(2)

    block.locator("[data-collab-fill-form-panel]").click()
    expect(panel.locator("input[name=collaborator_order_id]")).to_have_value("2834957")
    expect(panel.locator("input[name=price_paid_cents]")).to_have_value("571,64")
    expect(panel.locator("input[name=article_url]")).to_have_value(ARTICLE)
    panel.locator(".seo-choice-item", has_text="Размещено").click()
    panel.locator("[data-panel-save]").click()

    expect(panel).to_be_hidden()
    expect(page.locator("[data-collab-tab='ready'] b")).to_have_text("0")
    assert same_document(page)
    placement.refresh_from_db()
    assert placement.status == PlacementStatus.PLACED
    assert (placement.collaborator_order_id, placement.price_paid_cents) == ("2834957", 57164)
    assert placement.article_url == ARTICLE
    assert placement.published_at is not None
    assert timezone.localtime(placement.published_at).date() == timezone.localdate()


def test_cancelled_deal_opens_rejected(
    admin_page: Page, live_server: LiveServer, placement: Placement, api: list[dict[str, Any]]
) -> None:
    api[:] = [item(status="Rejected by the owner")]
    deals.refresh(FakeClient(api))  # type: ignore[arg-type]
    page = admin_page
    page.goto(f"{live_server.url}/admin/collaborator/#differ")

    page.locator("#collab-differ [data-collab-status]", has_text="Отказ").click()

    panel = page.locator(PANEL)
    expect(panel.locator("input[name=status][value=rejected]")).to_be_checked()
    expect(panel.locator("textarea[name=comment]")).to_be_focused()
    panel.locator("textarea[name=comment]").fill("Rejected by the owner: не тот формат")
    panel.locator("[data-panel-save]").click()

    # Экран перечитан, а вкладка та же — «Расходится», и строки там больше нет.
    expect(panel).to_be_hidden()
    expect(page.locator("[data-collab-tab='differ']")).to_have_class(re.compile("is-active"))
    expect(page.locator("#collab-differ")).to_be_visible()
    expect(page.locator("#collab-differ .seo-collab-empty")).to_have_text("Расхождений нет.")
    assert page.url.endswith("#differ")
    placement.refresh_from_db()
    assert placement.status == PlacementStatus.REJECTED
    assert placement.comment == "Rejected by the owner: не тот формат"


def test_tab_survives_actions(
    admin_page: Page, live_server: LiveServer, placement: Placement, api: list[dict[str, Any]]
) -> None:
    PlacementLink.objects.filter(placement=placement, link_index=2).update(anchor="Converito")
    deals.refresh(FakeClient(api))  # type: ignore[arg-type]
    page = admin_page
    page.goto(f"{live_server.url}/admin/collaborator/")
    mark(page)
    page.locator("[data-collab-tab='differ']").click()

    page.locator("#collab-differ [data-collab-keep]").first.click()

    expect(page.locator(".seo-toast-success, .seo-toast-info")).to_contain_text("Оставлено")
    expect(page.locator("[data-collab-tab='kept'] b")).to_have_text("1")
    expect(page.locator("#collab-differ")).to_be_visible()
    assert page.url.endswith("#differ")
    assert same_document(page)
