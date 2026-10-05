"""Карточка площадки (E9-13, ADR-058): раскладка, лента заметок, свёрнутые разделы."""

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.sites import offers, site_card
from apps.sites.models import Product, ProductSite, Seller, Site, SiteMetric, SiteNote
from apps.workspace import card
from apps.workspace.models import UserSettings
from apps.workspace.products import choose_product
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db

SECTIONS_URL = reverse("admin:card_sections")


def _toggle(client: Client, section: str, closed: bool) -> list[str]:
    response = client.post(SECTIONS_URL, {"section": section, "closed": "1" if closed else "0"})
    assert response.status_code == 200
    keys: list[str] = response.json()["closed"]
    return keys


# ---------- Свёрнутые разделы ----------


def test_nothing_closed_by_default(admin_user: User) -> None:
    assert card.closed_sections(admin_user) == frozenset()


def test_close_and_open_one_section(admin_client: Client, admin_user: User) -> None:
    assert _toggle(admin_client, "data", True) == ["data"]
    assert _toggle(admin_client, "notes", True) == ["notes", "data"]
    assert card.closed_sections(admin_user) == {"notes", "data"}
    assert _toggle(admin_client, "data", False) == ["notes"]


def test_close_all_and_open_all(admin_client: Client) -> None:
    assert _toggle(admin_client, "all", True) == list(card.SECTIONS)
    assert _toggle(admin_client, "all", False) == []


def test_other_tab_choice_kept(admin_client: Client, admin_user: User) -> None:
    # Вторая вкладка сворачивает своё — свёрнутое в первой остаётся.
    _toggle(admin_client, "price", True)
    _toggle(admin_client, "gray", True)
    assert card.closed_sections(admin_user) == {"price", "gray"}


def test_choice_keeps_working_product(admin_client: Client, admin_user: User) -> None:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    UserSettings.objects.create(user=admin_user, product=product)
    _toggle(admin_client, "data", True)
    row = UserSettings.objects.get(user=admin_user)
    assert row.product == product
    assert row.card_closed == ["data"]


def test_choice_is_per_user(admin_client: Client, django_user_model: type[User]) -> None:
    other = django_user_model.objects.create_user("kate", password="x", is_staff=True)
    _toggle(admin_client, "notes", True)
    assert card.closed_sections(other) == frozenset()


def test_unknown_section_rejected(admin_client: Client, admin_user: User) -> None:
    response = admin_client.post(SECTIONS_URL, {"section": "price,notes", "closed": "1"})
    assert response.status_code == 400
    assert not UserSettings.objects.filter(user=admin_user).exists()


def test_unknown_stored_key_ignored(admin_user: User) -> None:
    # Раздел убрали из карточки, а у пользователя он остался свёрнутым.
    UserSettings.objects.create(user=admin_user, card_closed=["old", "gray"])
    assert card.closed_sections(admin_user) == {"gray"}


def test_sections_need_login(client: Client) -> None:
    response = client.post(SECTIONS_URL, {"section": "data", "closed": "1"})
    assert response.status_code == 302
    assert not UserSettings.objects.exists()


def test_sections_only_post(admin_client: Client) -> None:
    assert admin_client.get(SECTIONS_URL).status_code == 405


# ---------- Подписи ленты заметок ----------

NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)  # 15:00 по Москве


@pytest.mark.parametrize(
    ("moment", "text"),
    [
        (dt.datetime(2026, 10, 5, 9, 45, tzinfo=dt.UTC), "сегодня, 12:45"),
        (dt.datetime(2026, 10, 4, 15, 2, tzinfo=dt.UTC), "вчера, 18:02"),
        # 21:30 UTC 03.10 — уже 04.10 по Москве: «вчера».
        (dt.datetime(2026, 10, 3, 21, 30, tzinfo=dt.UTC), "вчера, 00:30"),
        (dt.datetime(2026, 10, 3, 15, 2, tzinfo=dt.UTC), "3 октября, 18:02"),
        (dt.datetime(2025, 12, 31, 9, 0, tzinfo=dt.UTC), "31 декабря 2025, 12:00"),
    ],
)
def test_when_text(moment: dt.datetime, text: str) -> None:
    assert site_card.when_text(moment, NOW) == text


@pytest.mark.parametrize(
    ("count", "text"),
    [(1, "1 поле"), (2, "2 поля"), (5, "5 полей"), (11, "11 полей"), (21, "21 поле")],
)
def test_fields_text(count: int, text: str) -> None:
    assert site_card.fields_text(count) == text


# ---------- Карточка на экране ----------


@pytest.fixture
def products() -> tuple[Product, Product]:
    return (
        Product.objects.create(name="Convertio", domain="convertio.co"),
        Product.objects.create(name="Clideo", domain="clideo.com"),
    )


@pytest.fixture
def site(products: tuple[Product, Product]) -> Site:
    site = Site.objects.create(domain="hochgepokert.com", language="de")
    SiteMetric.objects.create(site=site, dr=81, organic_traffic=7303)
    for product in products:
        ProductSite.objects.get_or_create(site=site, product=product)
    return site


def _card(client: Client, site: Site, partial: bool = True) -> str:
    url = reverse("admin:sites_site_card", args=[site.pk])
    response = client.get(url, headers={PARTIAL_HEADER: "1"} if partial else {})
    assert response.status_code == 200
    return response.content.decode()


def _section(page: str, key: str) -> str:
    start = page.index(f'data-section="{key}"')
    return page[page.rindex("<section", 0, start) : page.index("</section>", start)]


def test_facts_in_head(admin_client: Client, site: Site) -> None:
    page = _card(admin_client, site)
    head = page[page.index("seo-card-head") : page.index("</header>")]
    assert "<dt>DR</dt><dd>81</dd>" in head
    assert "7\u00a0303" in head  # неразрывный пробел, как в «Площадках»
    assert "<dt>Язык</dt><dd>de</dd>" in head
    assert "не выбрана" in head  # рабочей цены нет


def test_note_shows_nick_product_and_time(
    admin_client: Client, admin_user: User, site: Site, products: tuple[Product, Product]
) -> None:
    body = "Просили не ставить ссылку в первый абзац"
    offers.add_note(site, body, product=products[0], author=admin_user)
    feed = _section(_card(admin_client, site), "notes")
    note = feed[feed.index('<li class="seo-note">') :]
    assert ">A</span>" in note  # буква ника admin
    assert "<b>admin</b>" in note
    assert '<span class="seo-chip seo-info">Convertio</span>' in note
    assert "сегодня, " in note
    assert "Просили не ставить ссылку" in note


def test_system_note_differs(admin_client: Client, site: Site) -> None:
    body = "Рабочая цена: €520 → €490"
    SiteNote.objects.create(site=site, body=body, source="прайс LinkHub 02.10")
    feed = _section(_card(admin_client, site), "notes")
    note = feed[feed.index('<li class="seo-note seo-note-system">') :]
    assert "<b>Система</b>" in note
    assert "прайс LinkHub 02.10" in note


def test_working_product_first_and_in_note_form(
    admin_client: Client, admin_user: User, site: Site, products: tuple[Product, Product]
) -> None:
    convertio, clideo = products
    choose_product(admin_user, clideo)
    page = _card(admin_client, site)
    decision = page[page.index('class="seo-card-decision"') :]
    assert decision.index("Clideo") < decision.index("Convertio")
    notes = _section(page, "notes")
    assert f'<option value="{clideo.pk}" selected>Clideo</option>' in notes
    assert f'<option value="{convertio.pk}">Convertio</option>' in notes


def test_all_open_by_default(admin_client: Client, site: Site) -> None:
    page = _card(admin_client, site)
    assert "seo-closed" not in page
    assert page.count('aria-expanded="true"') == len(card.SECTIONS)
    assert "Свернуть все" in page


def test_closed_sections_on_every_card(admin_client: Client, admin_user: User, site: Site) -> None:
    other = Site.objects.create(domain="other.com")
    card.set_closed(admin_user, ("notes", "data"), closed=True)
    for each in (site, other):
        page = _card(admin_client, each)
        assert "seo-closed" in _section(page, "notes")
        assert "seo-closed" in _section(page, "data")
        assert "seo-closed" not in _section(page, "price")
        assert "seo-closed" not in _section(page, "gray")
    # Отдельная страница — так же.
    assert "seo-closed" in _section(_card(admin_client, site, partial=False), "notes")


def test_all_closed_offers_open_all(admin_client: Client, admin_user: User, site: Site) -> None:
    card.set_closed(admin_user, card.SECTIONS, closed=True)
    assert "Развернуть все" in _card(admin_client, site)


def test_summaries_for_closed(
    admin_client: Client, admin_user: User, site: Site, offer: Any
) -> None:
    offer(site, 49000, currency="EUR", extra={"Symbol": "Usd", "Sponsored": "yes"})
    offers.add_note(site, "Позвонить", author=admin_user)
    page = _card(admin_client, site)
    assert "€490 · Collaborator · предложений: 1" in _section(page, "price")
    assert "последняя — admin, сегодня, " in _section(page, "notes")
    assert "Collaborator · 2 поля" in _section(page, "data")
    assert "замеров нет" in _section(page, "gray")


def test_gray_price_column_only_when_known(admin_client: Client, site: Site, offer: Any) -> None:
    offer(site, 20000)
    assert "<th>Серая</th>" not in _card(admin_client, site)
    grey = Seller.objects.create(name="Grey Links")
    offer(site, 30000, seller=grey, gray_cents=40000, working=False)
    assert "<th>Серая</th>" in _card(admin_client, site)


def test_page_has_same_layout(admin_client: Client, site: Site) -> None:
    page = _card(admin_client, site, partial=False)
    assert '<h1 class="seo-panel-title seo-card-title">' in page
    assert "seo-card-facts" in page and "seo-card-jump" in page
    assert "data-panel-close" not in page  # закрывать на странице нечего
    panel = _card(admin_client, site)
    assert '<h2 class="seo-panel-title seo-card-title">' in panel
    assert "data-panel-close" in panel and "Открыть полностью" in panel
