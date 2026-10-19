"""Повторные размещения продукта на одной площадке (ADR-073).

Вкладки «Площадка · №1 · №2 · + Ещё размещение», новое размещение через
выбранное предложение продавца (оно же — план «Экономики»), сотрудник по входу,
удаление корзиной на вкладке, чужой продукт — только просмотр.
"""

import datetime as dt
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.content.models import DomainSetting
from apps.placements import economics, other_products, repeat
from apps.placements.models import Placement, PlacementStatus, PlacementStatusChange
from apps.sites import latest, sellers
from apps.sites import offers as sellers_offers
from apps.sites.models import (
    ExchangeRate,
    Product,
    ProductSite,
    ProductSiteLatest,
    Seller,
    Site,
    SiteNote,
    SitePrice,
    WorkStatus,
)

pytestmark = pytest.mark.django_db

OfferFactory = Callable[..., SitePrice]
PARTIAL = {"X-Seo-Partial": "1"}
ADD = reverse("admin:placements_placement_add")
PLACEMENTS = reverse("admin:placements_placement_changelist")
SITES = reverse("admin:sites_productsitelatest_changelist")


@pytest.fixture
def convertio() -> Product:
    # Первый активный продукт — рабочий у того, кто продукт в шапке не выбирал.
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo(convertio: Product) -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def site(convertio: Product, clideo: Product) -> Site:
    return Site.objects.create(domain="spacecoastdaily.com")


@pytest.fixture
def working(site: Site, offer: OfferFactory) -> SitePrice:
    """Рабочая цена: Collaborator, публикация, €126.50."""
    return offer(site, 12650)


@pytest.fixture
def cheaper(site: Site, offer: OfferFactory, working: SitePrice) -> SitePrice:
    """Другой продавец дешевле рабочей: LinkHouse, €95."""
    return offer(site, 9500, seller=Seller.objects.create(name="LinkHouse"), working=False)


def _change(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _new(site: Site, product: Product, **fields: Any) -> Placement:
    return Placement.objects.create(site=site, product=product, **fields)


def _add_form(site: Site, product: Product, **fields: str) -> dict[str, str]:
    """Форма нового размещения из вкладок, как её отправляет браузер."""
    data = {
        "site": str(site.pk),
        "product": str(product.pk),
        "status": "in_work",
        "currency": "EUR",
        "links-TOTAL_FORMS": "0",
        "links-INITIAL_FORMS": "0",
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
    data.update(fields)
    return data


class TestTabs:
    def test_numbers_follow_creation_order(
        self, site: Site, convertio: Product, working: SitePrice
    ) -> None:
        first = _new(site, convertio, status=PlacementStatus.ORDERED, seller=working.seller)
        second = _new(site, convertio)
        tabs = repeat.tabs(site.pk, convertio.pk, second.pk, can_add=True, can_delete=True)
        assert [(tab.number, tab.pk) for tab in tabs.shown] == [(1, first.pk), (2, second.pk)]
        assert [tab.selected for tab in tabs.shown] == [False, True]
        assert tabs.shown[0].seller == "Collaborator"
        assert tabs.shown[0].status == "Заявка отправлена"
        assert tabs.shown[0].tone == "live"
        assert tabs.site_url == reverse("admin:sites_site_card", args=[site.pk])
        assert tabs.site_sub == "2 размещения"
        assert tabs.add_url == f"{ADD}?site={site.pk}&product={convertio.pk}"
        assert tabs.add_label == "+ Ещё размещение"
        assert tabs.shown[1].delete_url == reverse(
            "admin:placements_placement_delete", args=[second.pk]
        )

    def test_first_placement_is_taken(self, site: Site, convertio: Product) -> None:
        tabs = repeat.tabs(site.pk, convertio.pk, "site", can_add=True, can_delete=True)
        assert tabs.shown == [] and tabs.site_selected
        assert tabs.add_label == "+ Взять в размещение"
        assert tabs.site_sub == "размещений нет"

    def test_without_rights_no_trash_and_no_adding(self, site: Site, convertio: Product) -> None:
        _new(site, convertio)
        tabs = repeat.tabs(site.pk, convertio.pk, "site", can_add=False, can_delete=False)
        assert tabs.add_url is None and tabs.shown[0].delete_url is None

    def test_many_hide_the_early_ones(self, site: Site, convertio: Product) -> None:
        for _ in range(8):
            _new(site, convertio, status=PlacementStatus.PLACED)
        _new(site, convertio, status=PlacementStatus.REJECTED)
        live = _new(site, convertio, status=PlacementStatus.ORDERED)
        tabs = repeat.tabs(site.pk, convertio.pk, "site", can_add=True, can_delete=True)
        # Видны последние два (среди них идущее), ранние — в «Ещё 8 ▾».
        assert [tab.number for tab in tabs.shown] == [9, 10]
        assert tabs.shown[-1].pk == live.pk
        assert [tab.number for tab in tabs.hidden] == list(range(1, 9))
        assert tabs.hidden_range == "№1–№8"

    def test_open_early_one_stays_visible(self, site: Site, convertio: Product) -> None:
        early = [_new(site, convertio, status=PlacementStatus.PLACED) for _ in range(6)]
        tabs = repeat.tabs(site.pk, convertio.pk, early[0].pk, can_add=True, can_delete=True)
        assert tabs.shown[0].pk == early[0].pk and tabs.shown[0].selected

    def test_blacklist_blocks_adding(self, site: Site, convertio: Product) -> None:
        ProductSite.objects.filter(site=site, product=convertio).update(
            status=WorkStatus.BLACKLISTED
        )
        tabs = repeat.tabs(site.pk, convertio.pk, "site", can_add=True, can_delete=True)
        assert tabs.add_url is None
        assert "чёрном списке у Convertio" in tabs.blocked


class TestCurrentAndDates:
    def test_current_is_the_running_one(self, site: Site, convertio: Product) -> None:
        placed = _new(site, convertio, status=PlacementStatus.PLACED)
        running = _new(site, convertio, status=PlacementStatus.ORDERED)
        _new(site, convertio, status=PlacementStatus.REJECTED)
        assert repeat.current(repeat.of_site(site.pk, convertio.pk)) == running
        running.status = PlacementStatus.PLACED
        running.save()
        # Идущих нет — последнее.
        last = repeat.current(repeat.of_site(site.pk, convertio.pk))
        assert last is not None and last.pk not in (placed.pk, running.pk)

    def test_status_date_from_history(self, site: Site, convertio: Product) -> None:
        placement = _new(site, convertio)
        placement.status = PlacementStatus.ORDERED
        placement.save()
        changed = (
            PlacementStatusChange.objects.filter(placement=placement, to_status="ordered")
            .values_list("changed_at", flat=True)
            .get()
        )
        assert repeat.status_dates([placement])[placement.pk] == changed

    def test_status_date_without_history(self, site: Site, convertio: Product) -> None:
        published = timezone.make_aware(dt.datetime(2025, 5, 12, 12, 0))
        placement = _new(site, convertio, status=PlacementStatus.PLACED, published_at=published)
        PlacementStatusChange.objects.filter(placement=placement).delete()
        assert repeat.status_dates([placement])[placement.pk] == published

    def test_icon_opens_the_running_placement(self, site: Site, convertio: Product) -> None:
        # Значок у домена и «другие продукты» открывают идущее размещение, а не
        # первое опубликованное.
        _new(
            site,
            convertio,
            status=PlacementStatus.PLACED,
            published_at=timezone.make_aware(dt.datetime(2026, 3, 14)),
        )
        running = _new(site, convertio, status=PlacementStatus.ORDERED)
        rows = other_products.by_site([site.pk])[site.pk]
        assert other_products.own(rows, convertio.pk) == running.pk


class TestAddForm:
    def test_offers_with_working_first(
        self, admin_client: Client, site: Site, convertio: Product, cheaper: SitePrice
    ) -> None:
        page = admin_client.get(
            ADD, {"site": site.pk, "product": convertio.pk}, headers=PARTIAL
        ).content.decode()
        assert "Через кого" in page
        assert "spacecoastdaily.com — новое размещение" in page
        assert "Collaborator · публикация · €126.50" in page and "— рабочая" in page
        assert "LinkHouse · публикация · €95 · " in page
        assert "▼ дешевле рабочей на €31.50" in page
        # Площадка и продукт заданы вкладкой — в панели не поля.
        assert 'type="hidden" name="site"' in page and 'type="hidden" name="product"' in page

    def test_parallel_placement_through_chosen_offer(
        self,
        admin_client: Client,
        admin_user: User,
        site: Site,
        convertio: Product,
        working: SitePrice,
        cheaper: SitePrice,
    ) -> None:
        first = _new(site, convertio, status=PlacementStatus.ORDERED, price=working)
        url = f"{ADD}?site={site.pk}&product={convertio.pk}"
        response = admin_client.post(
            url,
            # Сотрудник в форме уже выбран — тот, кто вошёл; браузер его и шлёт.
            _add_form(site, convertio, offer=str(cheaper.pk), employee=str(admin_user.pk)),
            headers=PARTIAL,
        )
        data = json.loads(response.content)
        new = Placement.objects.exclude(pk=first.pk).get(site=site, product=convertio)
        # Из панели вкладок — сразу новое размещение в той же панели.
        assert data["saved"] and data["next"] == _change(new)
        assert (new.price_id, new.seller_id) == (cheaper.pk, cheaper.seller_id)
        assert new.placement_type == "guest_post" and new.status == PlacementStatus.IN_WORK
        assert new.employee_id == admin_user.pk
        # Рабочая цена площадки та же: «Сделать рабочей» не отмечено.
        site.refresh_from_db()
        assert site.price_id == working.pk

    def test_make_chosen_offer_working(
        self, admin_client: Client, site: Site, convertio: Product, cheaper: SitePrice
    ) -> None:
        admin_client.post(
            f"{ADD}?site={site.pk}&product={convertio.pk}",
            _add_form(site, convertio, offer=str(cheaper.pk), make_working="on"),
            headers=PARTIAL,
        )
        site.refresh_from_db()
        assert site.price_id == cheaper.pk
        assert SiteNote.objects.filter(site=site, body__startswith="Цена:").exists()

    def test_employee_prefilled(
        self, admin_client: Client, admin_user: User, site: Site, convertio: Product
    ) -> None:
        response = admin_client.get(ADD, {"site": site.pk, "product": convertio.pk})
        assert response.context["adminform"].form["employee"].value() == admin_user.pk

    def test_blacklisted_site_is_refused(
        self, admin_client: Client, site: Site, convertio: Product, working: SitePrice
    ) -> None:
        ProductSite.objects.filter(site=site, product=convertio).update(
            status=WorkStatus.BLACKLISTED
        )
        response = admin_client.post(
            f"{ADD}?site={site.pk}&product={convertio.pk}",
            _add_form(site, convertio, offer=str(working.pk)),
        )
        assert response.status_code == 200
        assert "чёрном списке у Convertio" in response.content.decode()
        assert not Placement.objects.filter(site=site).exists()

    def test_plain_add_keeps_seller_fields(
        self, admin_client: Client, site: Site, convertio: Product, cheaper: SitePrice
    ) -> None:
        # «Размещения → Добавить» без площадки в адресе: площадку выбрали в форме —
        # «Через кого» не было на экране, продавец и услуга — полями, как раньше.
        seller = Seller.objects.create(name="Picked by hand")
        response = admin_client.post(
            ADD,
            _add_form(site, convertio, seller=str(seller.pk), placement_type="link_insertion"),
        )
        assert response.status_code == 302
        placement = Placement.objects.get(site=site)
        assert (placement.seller_id, placement.placement_type) == (seller.pk, "link_insertion")
        assert placement.price_id is None

    def test_only_working_product_on_offer(
        self, admin_client: Client, site: Site, convertio: Product, clideo: Product
    ) -> None:
        response = admin_client.get(ADD)
        choices = response.context["adminform"].form.fields["product"].queryset
        assert list(choices) == [convertio]


class TestOtherProductIsReadOnly:
    def test_placement_opens_but_does_not_save(
        self, admin_client: Client, site: Site, clideo: Product
    ) -> None:
        theirs = _new(site, clideo, status=PlacementStatus.ORDERED)
        page = admin_client.get(_change(theirs), headers=PARTIAL).content.decode()
        assert "data-panel-save" not in page
        response = admin_client.post(_change(theirs), {"status": "placed"}, headers=PARTIAL)
        assert response.status_code == 403
        theirs.refresh_from_db()
        assert theirs.status == PlacementStatus.ORDERED
        delete = reverse("admin:placements_placement_delete", args=[theirs.pk])
        assert admin_client.get(delete).status_code == 403

    def test_decision_of_other_product_is_refused(
        self, admin_client: Client, site: Site, clideo: Product
    ) -> None:
        row = ProductSite.objects.get(site=site, product=clideo)
        url = reverse("admin:sites_productsite_decision", args=[row.pk])
        response = admin_client.post(url, {"status": "blacklisted"}, headers=PARTIAL)
        assert response.status_code == 403
        row.refresh_from_db()
        assert row.status == WorkStatus.NEW

    def test_card_shows_other_product_without_change(
        self, admin_client: Client, site: Site, convertio: Product, clideo: Product
    ) -> None:
        theirs = _new(
            site,
            clideo,
            status=PlacementStatus.PLACED,
            article_url="https://spacecoastdaily.com/clideo-review/",
        )
        card = reverse("admin:sites_site_card", args=[site.pk])
        page = admin_client.get(card, headers=PARTIAL).content.decode()
        other = page[page.index('class="seo-card-others"') : page.index('class="seo-card-jump"')]
        assert "Открыть статью" in other
        assert f'href="{_change(theirs)}" data-panel' in other
        assert "только просмотр" in other and ">сменить<" not in other
        # Вкладки — рабочего продукта: Clideo среди них нет.
        assert 'class="seo-ptabs"' in page and "+ Взять в размещение" in page

    def test_status_action_skips_other_product(
        self, admin_client: Client, site: Site, convertio: Product, clideo: Product
    ) -> None:
        rows = [str(pk) for pk in ProductSiteLatest.objects.values_list("pk", flat=True)]
        admin_client.post(
            SITES + "?list=all",
            {"action": "set_status_action", "_selected_action": rows, "status": "viewed"},
            follow=True,
        )
        assert ProductSite.objects.get(site=site, product=convertio).status == WorkStatus.VIEWED
        assert ProductSite.objects.get(site=site, product=clideo).status == WorkStatus.NEW


class TestDeletion:
    def test_no_bulk_delete_in_placements(self, admin_client: Client, convertio: Product) -> None:
        response = admin_client.get(PLACEMENTS)
        choices = [value for value, _ in response.context["action_form"].fields["action"].choices]
        assert "delete_selected" not in choices

    def test_trash_on_the_tab_deletes_from_panel(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        first = _new(site, convertio, status=PlacementStatus.PLACED)
        second = _new(site, convertio)
        page = admin_client.get(_change(second), headers=PARTIAL).content.decode()
        delete = reverse("admin:placements_placement_delete", args=[second.pk])
        assert f'class="seo-ptab-trash" href="{delete}" data-panel' in page
        confirm = admin_client.get(delete, headers=PARTIAL).content.decode()
        # «Отмена» в панели — назад к размещению, а не шаг назад по странице.
        assert f'href="{_change(second)}" class="seo-btn" data-panel>Отмена' in confirm
        response = admin_client.post(delete, {"post": "yes"}, headers=PARTIAL)
        assert json.loads(response.content)["deleted"] is True
        assert list(Placement.objects.filter(site=site)) == [first]


class TestEconomics:
    def _plan(self, product: Product) -> dict[int, int | None]:
        return {row.id: row.eur_cents for row in economics.rows(product.pk)}

    @pytest.mark.parametrize(
        ("writing", "threshold", "currency"),
        [
            (None, 50, "EUR"),
            (3000, 50, "EUR"),
            (8000, 50, "EUR"),
            (2343, None, "EUR"),
            (3000, 50, "USD"),
        ],
    )
    def test_own_price_plans_like_the_view(
        self,
        site: Site,
        convertio: Product,
        offer: OfferFactory,
        writing: int | None,
        threshold: int | None,
        currency: str,
    ) -> None:
        # Та же формула, что у expected_spend_cents в v_product_site_latest.
        if threshold is not None:
            DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": threshold})
        if currency != "EUR":
            ExchangeRate.objects.create(
                currency=currency, rate_date=dt.date(2026, 10, 1), rate=Decimal("1.25")
            )
        price = offer(site, 12650, writing_cents=writing, announce_cents=1000, currency=currency)
        placement = _new(site, convertio, price=price)
        view = ProductSiteLatest.objects.get(site_id=site.pk, product=convertio)
        assert self._plan(convertio)[placement.pk] == view.expected_spend_cents

    def test_working_price_switch_keeps_own_plan(
        self,
        site: Site,
        convertio: Product,
        working: SitePrice,
        cheaper: SitePrice,
    ) -> None:
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        mine = _new(site, convertio, price=working)
        old = _new(site, convertio)  # до ADR-073: своей цены нет — по рабочей
        Site.all_objects.filter(pk=site.pk).update(price=cheaper)
        latest.refresh([site.pk])
        plan = self._plan(convertio)
        assert plan[mine.pk] == 12650
        assert plan[old.pk] == 9500

    def test_old_placement_plans_by_its_seller(
        self,
        site: Site,
        convertio: Product,
        working: SitePrice,
        cheaper: SitePrice,
    ) -> None:
        # Opentools.ai, 18.10.2026: размещение до ADR-073 через Collaborator,
        # рабочую цену потом сменили — план остаётся по его продавцу.
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        old = _new(site, convertio, seller=working.seller, placement_type="guest_post")
        Site.all_objects.filter(pk=site.pk).update(price=cheaper)
        latest.refresh([site.pk])
        assert self._plan(convertio)[old.pk] == 12650
        assert repeat.offers_of([old])[old.pk] == working

    def test_seller_without_price_has_no_plan(
        self, site: Site, convertio: Product, working: SitePrice
    ) -> None:
        # Zain MediaX, 19.10.2026: заявка через продавца без цены на площадку —
        # суммы нет, пока не впишут «Заплачено»; цену рабочей цены не берём.
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        zain = Seller.objects.create(name="Zain MediaX")
        deal = _new(
            site, convertio, seller=zain, placement_type="guest_post",
            status=PlacementStatus.ORDERED,
        )  # fmt: skip
        assert self._plan(convertio)[deal.pk] is None
        assert repeat.offers_of([deal]) == {}
        Placement.objects.filter(pk=deal.pk).update(price_paid_cents=30000, currency="EUR")
        assert self._plan(convertio)[deal.pk] == 30000

    def test_old_placement_price_of_its_day(
        self, site: Site, convertio: Product, offer: OfferFactory
    ) -> None:
        # Цена продавца на день заведения; позже она поменялась — план прежний.
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        then = offer(site, 10000, checked_at=timezone.now() - dt.timedelta(days=10))
        old = _new(site, convertio, seller=then.seller, placement_type="guest_post")
        # Завели пять дней назад: в тесте у всех записей одно время транзакции.
        Placement.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=5))
        offer(site, 15000)
        latest.refresh([site.pk])
        assert self._plan(convertio)[old.pk] == 10000
        assert repeat.offers_of([old])[old.pk] == then
        # Цены продавца до заведения нет — первая после.
        fresh = Site.objects.create(domain="fresh.example")
        late = _new(fresh, convertio, seller=then.seller, placement_type="guest_post")
        Placement.objects.filter(pk=late.pk).update(
            created_at=timezone.now() - dt.timedelta(days=1)
        )
        first = offer(fresh, 11000, working=False)
        offer(fresh, 12000, working=False)
        assert repeat.offers_of([late])[late.pk] == first
        assert self._plan(convertio)[late.pk] == 11000


class TestOwnSeller:
    """Продавец размещения меняется только руками в его карточке (19.10.2026)."""

    def test_manual_seller_change_switches_offer(
        self,
        admin_client: Client,
        site: Site,
        convertio: Product,
        working: SitePrice,
        cheaper: SitePrice,
    ) -> None:
        placement = _new(
            site, convertio, seller=working.seller, placement_type="guest_post", price=working
        )
        data = {
            "status": "in_work",
            "placement_type": "guest_post",
            "seller": str(cheaper.seller_id),
            "employee": "",
            "collaborator_order_id": "",
            "ordered_at": "",
            "price_paid_cents": "",
            "currency": "EUR",
            "article_url": "",
            "published_at": "",
            "announce_on_homepage": "unknown",
            "clicks_from_homepage": "",
            "comment": "",
            "links-TOTAL_FORMS": "0",
            "links-INITIAL_FORMS": "0",
            "links-MIN_NUM_FORMS": "0",
            "links-MAX_NUM_FORMS": "1000",
        }
        response = admin_client.post(_change(placement), data, headers=PARTIAL)
        assert response.status_code == 200 and response.json()["saved"]
        placement.refresh_from_db()
        assert (placement.seller_id, placement.price_id) == (cheaper.seller_id, cheaper.pk)

    def test_working_price_change_keeps_seller(
        self, site: Site, convertio: Product, working: SitePrice, cheaper: SitePrice
    ) -> None:
        placement = _new(site, convertio, seller=working.seller, price=working)
        sellers_offers.set_working_price(cheaper)
        placement.refresh_from_db()
        assert (placement.seller_id, placement.price_id) == (working.seller_id, working.pk)


class TestSellerMerge:
    def test_twin_prices_keep_the_placement(
        self, site: Site, convertio: Product, offer: OfferFactory
    ) -> None:
        main = Seller.objects.create(name="Athena")
        twin = Seller.objects.create(name="Athena S.")
        checked = timezone.now()
        keep = offer(site, 9000, seller=main, working=False, checked_at=checked)
        extra = offer(site, 9000, seller=twin, working=False, checked_at=checked)
        placement = _new(site, convertio, price=extra)
        sellers.merge(main, [twin])
        placement.refresh_from_db()
        assert placement.price_id == keep.pk
        assert not SitePrice.objects.filter(pk=extra.pk).exists()
