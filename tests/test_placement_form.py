"""Форма размещения под удобство (E9-11): панель и полная страница.

Статус кнопками по порядку, даты без времени, «Заплачено» в валюте; в
панели площадка и продукт — в заголовке, кнопка проверки — в «Проверках».
"""

import datetime as dt
import re

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements.forms import PlacementForm
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, ProductSite, Site, SiteStatus
from config.forms import DayFieldsForm, MoneyField, start_of_day

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}
MSK = dt.timezone(dt.timedelta(hours=3))


@pytest.fixture
def placement() -> Placement:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="example.com")
    return Placement.objects.create(
        site=site,
        product=product,
        article_url="https://example.com/blog/post/",
        published_at=dt.datetime(2026, 10, 1, 15, 30, tzinfo=MSK),
        price_paid_cents=12050,
    )


def _url(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _panel_form(**fields: str) -> dict[str, str]:
    """Форма панели, как её отправляет браузер: без площадки и продукта."""
    data = {
        "status": "in_work",
        "placement_type": "",
        "seller": "",
        "employee": "",
        "collaborator_order_id": "",
        "ordered_at": "",
        "price_paid_cents": "120,50",
        "currency": "EUR",
        "article_url": "https://example.com/blog/post/",
        "published_at": "2026-10-01",
        "announce_on_homepage": "unknown",
        "clicks_from_homepage": "",
        "comment": "",
        "links-TOTAL_FORMS": "0",
        "links-INITIAL_FORMS": "0",
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
    data.update(fields)
    return data


class TestMoney:
    @pytest.mark.parametrize(
        ("typed", "cents"), [("120,50", 12050), ("120.5", 12050), ("99", 9900), ("", None)]
    )
    def test_typed_in_units(self, typed: str, cents: int | None) -> None:
        assert MoneyField(required=False).clean(typed) == cents

    def test_negative_is_error(self) -> None:
        field = MoneyField(required=False)
        with pytest.raises(ValidationError, match="равно 0"):
            field.clean("-5")

    def test_shown_in_units(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_url(placement)).content.decode()
        assert re.search(r'name="price_paid_cents" value="120,50"', page)

    def test_unchanged_amount_is_not_a_change(self, placement: Placement) -> None:
        assert not MoneyField(required=False).has_changed(12050, "120,50")
        assert MoneyField(required=False).has_changed(12050, "121")


class TestDay:
    def test_form_shows_day_only(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_url(placement)).content.decode()
        assert 'type="date" name="published_at" value="2026-10-01"' in page
        assert "data-today" in page
        # Ни поля времени, ни предупреждения о часовом поясе.
        assert "vTimeField" not in page and "timezonewarning" not in page

    def test_same_day_keeps_time(self, admin_client: Client, placement: Placement) -> None:
        response = admin_client.post(_url(placement), _panel_form(), headers=PARTIAL)
        assert response.json()["saved"] is True
        placement.refresh_from_db()
        assert placement.published_at == dt.datetime(2026, 10, 1, 15, 30, tzinfo=MSK)

    def test_new_day_is_start_of_day(self, admin_client: Client, placement: Placement) -> None:
        admin_client.post(
            _url(placement),
            _panel_form(published_at="2026-10-03", ordered_at="2026-09-30"),
            headers=PARTIAL,
        )
        placement.refresh_from_db()
        assert placement.published_at == start_of_day(dt.date(2026, 10, 3))
        assert timezone.localtime(placement.published_at).time() == dt.time(0, 0)
        assert placement.ordered_at == start_of_day(dt.date(2026, 9, 30))

    def test_cleared_day(self, admin_client: Client, placement: Placement) -> None:
        admin_client.post(_url(placement), _panel_form(published_at=""), headers=PARTIAL)
        placement.refresh_from_db()
        assert placement.published_at is None

    def test_wrong_day_is_form_error(self, admin_client: Client, placement: Placement) -> None:
        response = admin_client.post(
            _url(placement), _panel_form(published_at="31.02.2026"), headers=PARTIAL
        )
        assert response["Content-Type"].startswith("text/html")
        assert "errorlist" in response.content.decode()

    def test_unchanged_day_is_not_a_change(self, placement: Placement) -> None:
        form = PlacementForm(instance=placement)
        field = form.fields["published_at"]
        assert not field.has_changed(placement.published_at, "2026-10-01")
        assert field.has_changed(placement.published_at, "2026-10-02")
        assert issubclass(PlacementForm, DayFieldsForm)


class TestStatus:
    def _buttons(self, page: str) -> list[list[str]]:
        block = re.search(r'<div class="seo-choice".*?</div></div>', page, re.S)
        assert block is not None
        rows = re.findall(r'<div class="seo-choice-row">(.*?)</div>', block.group(), re.S)
        return [re.findall(r'value="([a-z_]+)"', row) for row in rows]

    def test_buttons_in_order(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        values = [status.value for status in PlacementStatus]
        # Два ряда: путь до публикации и отказы (ADR-062).
        assert self._buttons(page) == [values[:2], values[2:6], values[6:]]

    def test_order_and_publication_fill_their_day(
        self, admin_client: Client, placement: Placement
    ) -> None:
        page = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        assert re.search(r'value="ordered"[^>]*data-fills="ordered_at"', page)
        assert re.search(r'value="placed"[^>]*data-fills="published_at"', page)
        assert not re.search(r'value="writing"[^>]*data-fills', page)

    def test_status_from_panel_moves_site_status(
        self, admin_client: Client, placement: Placement
    ) -> None:
        """Статус из панели — тот же Placement.save(): площадка идёт за размещением (ADR-047)."""
        response = admin_client.post(
            _url(placement),
            _panel_form(status="ordered", ordered_at="2026-10-02"),
            headers=PARTIAL,
        )
        assert response.json()["message"] == "Размещение «example.com» — сохранено."
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.ORDERED
        decision = ProductSite.objects.get(site=placement.site, product=placement.product)
        assert decision.status == SiteStatus.ORDERED


class TestPanelLayout:
    def test_site_in_title(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        # Продукт — рабочий, из шапки: в заголовке карточки его нет (E1-19).
        assert '<h2 class="seo-panel-title">example.com</h2>' in page
        assert 'name="site"' not in page and 'name="product"' not in page
        card = reverse("admin:sites_site_card", args=[placement.site_id])
        assert f'<a href="{card}" data-panel>Карточка площадки</a>' in page

    def test_full_page_keeps_site_and_product(
        self, admin_client: Client, placement: Placement
    ) -> None:
        page = admin_client.get(_url(placement)).content.decode()
        assert 'name="site"' in page and 'name="product"' in page

    def test_groups_in_order(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        # Только форма: в меню страницы есть своя группа «Служебное».
        page = page[page.index('id="placement_form"') :]
        headings = re.findall(r'class="fieldset-heading">([^<]+)<', page)
        assert headings[:2] == ["Заявка", "Публикация"]
        # Ссылки статьи — между «Публикацией» и «Проверками».
        order = [">Публикация<", 'id="links-group"', ">Проверки<", ">Комментарий<", ">Служебное<"]
        positions = [page.index(mark) for mark in order]
        assert positions == sorted(positions)

    def test_check_button_in_checks_group(self, admin_client: Client, placement: Placement) -> None:
        panel = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        assert "data-indexation-button" in panel
        assert "data-indexation-form" not in panel
        full = admin_client.get(_url(placement)).content.decode()
        assert "data-indexation-form" in full
        assert "data-indexation-button" not in full

    def test_add_in_panel_asks_site_and_product(self, admin_client: Client) -> None:
        page = admin_client.get(
            reverse("admin:placements_placement_add"), headers=PARTIAL
        ).content.decode()
        assert '<h2 class="seo-panel-title">Размещение — новая запись</h2>' in page
        assert 'name="site"' in page and 'name="product"' in page


class TestCurrency:
    def test_currency_not_in_list_stays(self, admin_client: Client, placement: Placement) -> None:
        Placement.objects.filter(pk=placement.pk).update(currency="RUB")
        page = admin_client.get(_url(placement), headers=PARTIAL).content.decode()
        assert '<option value="RUB" selected>RUB</option>' in page


class TestList:
    def test_status_and_day_columns(self, admin_client: Client, placement: Placement) -> None:
        response = admin_client.get(reverse("admin:placements_placement_changelist"))
        page = response.content.decode()
        # data-seo-field — статус в строке меняется сразу после записи в панели.
        link = f'<a href="{_url(placement)}" title="Сменить статус" data-seo-field="status">'
        assert f"{link}В работе</a>" in page
        assert "01.10.2026" in page
        assert "15:30" not in page

    def test_list_is_panel_list(self, admin_client: Client) -> None:
        page = admin_client.get(reverse("admin:placements_placement_changelist")).content
        assert b"seo-panel-list" in page
