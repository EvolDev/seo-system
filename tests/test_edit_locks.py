"""Блокировка правки площадки и размещений (ADR-077).

Карточку площадки правит один человек — для всех продуктов; размещения
продукта на площадке — один человек внутри продукта. У второго запись только
для просмотра, с окошком «кто и с какого времени правит»; запись поверх чужой
блокировки не проходит; действия по отмеченным пропускают такие площадки.
Заметка — можно. Простой дольше EDIT_LOCK — блокировка не считается.
"""

import datetime as dt
import json
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.content.models import DomainSetting
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import (
    GrayScan,
    Product,
    ProductSite,
    ProductSiteLatest,
    Site,
    SiteNote,
    WorkStatus,
)
from apps.workspace import locks
from apps.workspace.models import EditLock
from apps.workspace.products import choose_product

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}
LOCK_URL = reverse("admin:edit_lock")
ADD = reverse("admin:placements_placement_add")
PLACEMENTS = reverse("admin:placements_placement_changelist")
SITES = reverse("admin:sites_productsitelatest_changelist")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo(convertio: Product) -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def site(convertio: Product, clideo: Product) -> Site:
    return Site.objects.create(domain="dci.com.br")


@pytest.fixture
def anna(convertio: Product) -> User:
    return User.objects.create_superuser("anna", "anna@example.com", "x", first_name="Анна")


@pytest.fixture
def boris(convertio: Product) -> User:
    return User.objects.create_superuser("boris", "boris@example.com", "x", first_name="Борис")


@pytest.fixture
def placement(site: Site, convertio: Product) -> Placement:
    return Placement.objects.create(site=site, product=convertio, status=PlacementStatus.IN_WORK)


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _idle(key: str, minutes: int) -> None:
    """Блокировка простаивает столько минут."""
    moment = timezone.now() - dt.timedelta(minutes=minutes)
    EditLock.objects.filter(key=key).update(seen_at=moment, started_at=moment)


def _form(site: Site, product: Product, **fields: str) -> dict[str, str]:
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


def _change(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _refused(response: Any) -> bool:
    """Отказ записи поверх чужой блокировки: JSON с locked (код 200, как у ошибки формы)."""
    return response.status_code == 200 and bool(json.loads(response.content).get("locked"))


def _gray(site: Site) -> dict[str, Any]:
    return {"total": "1000", "gray": "20", "total_query": f"site:{site.domain}"}


class TestRules:
    def test_first_edit_takes_second_sees_who(self, anna: User, boris: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        assert locks.take(anna, key) is None
        holder = locks.take(boris, key)
        assert holder is not None
        assert (holder.user_id, holder.name) == (anna.pk, "Анна")
        assert locks.state(boris, key) == (holder, False)
        assert locks.state(anna, key) == (None, True)
        assert locks.busy_text(key, holder).startswith("Площадку правит Анна с ")

    def test_next_edit_keeps_the_start(self, anna: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        locks.take(anna, key)
        start = timezone.now() - dt.timedelta(minutes=5)
        EditLock.objects.filter(key=key).update(started_at=start)
        locks.take(anna, key)
        assert EditLock.objects.get(key=key).started_at == start

    def test_idle_lock_goes_to_next_editor(self, anna: User, boris: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        locks.take(anna, key)
        _idle(key, 16)
        assert locks.take(boris, key) is None
        lock = EditLock.objects.get(key=key)
        assert lock.user_id == boris.pk
        assert lock.started_at > timezone.now() - dt.timedelta(minutes=1)

    def test_idle_time_from_setting(self, anna: User, boris: User, site: Site) -> None:
        DomainSetting.objects.filter(key="EDIT_LOCK", product=None).update(
            value={"idle_minutes": 60}
        )
        key = locks.site_key(site.pk)
        locks.take(anna, key)
        _idle(key, 16)
        holder = locks.take(boris, key)
        assert holder is not None and holder.user_id == anna.pk

    def test_touch_and_release_only_own(self, anna: User, boris: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        locks.take(anna, key)
        assert locks.touch(anna, key)
        assert not locks.touch(boris, key)
        locks.release(boris, key)
        assert EditLock.objects.get(key=key).user_id == anna.pk
        locks.release(anna, key)
        assert not EditLock.objects.filter(key=key).exists()
        assert locks.take(boris, key) is None


class TestPlacements:
    def test_second_sees_view_only_and_who(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        page = _client(boris).get(_change(placement), headers=PARTIAL).content.decode()
        assert "data-panel-save" not in page
        assert 'data-seo-lock-text="Размещения Convertio на этой площадке правит Анна с' in page
        assert "пока только просмотр." in page

    def test_save_over_lock_refused(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        response = _client(boris).post(
            _change(placement), _form(site, convertio, status="placed"), headers=PARTIAL
        )
        assert _refused(response)
        message = json.loads(response.content)["message"]
        assert "правит Анна с" in message and "изменения не записаны" in message
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.IN_WORK

    def test_full_page_save_over_lock_goes_back(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        response = _client(boris).post(_change(placement), _form(site, convertio, status="placed"))
        assert response.status_code == 302 and response["Location"] == _change(placement)
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.IN_WORK

    def test_save_takes_the_lock_and_holder_saves(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        key = locks.placements_key(site.pk, convertio.pk)
        response = _client(boris).post(
            _change(placement), _form(site, convertio, status="ordered"), headers=PARTIAL
        )
        assert json.loads(response.content)["saved"]
        assert EditLock.objects.get(key=key).user_id == boris.pk
        # Анна этой площадки не правит — её запись отказ получает.
        refused = _client(anna).post(
            _change(placement), _form(site, convertio, status="placed"), headers=PARTIAL
        )
        assert _refused(refused)

    def test_other_product_not_blocked(
        self, anna: User, boris: User, site: Site, convertio: Product, clideo: Product
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        theirs = Placement.objects.create(site=site, product=clideo, status=PlacementStatus.IN_WORK)
        choose_product(boris, clideo)
        response = _client(boris).post(
            _change(theirs), _form(site, clideo, status="ordered"), headers=PARTIAL
        )
        assert json.loads(response.content)["saved"]

    def test_new_placement_on_locked_site_refused(
        self, anna: User, boris: User, site: Site, convertio: Product
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        response = _client(boris).post(
            f"{ADD}?site={site.pk}&product={convertio.pk}",
            _form(site, convertio),
            headers=PARTIAL,
        )
        assert _refused(response)
        assert not Placement.objects.filter(site=site).exists()

    def test_delete_refused(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        url = reverse("admin:placements_placement_delete", args=[placement.pk])
        assert _refused(_client(boris).post(url, {"post": "yes"}, headers=PARTIAL))
        assert Placement.objects.filter(pk=placement.pk).exists()

    def test_bulk_status_skips_locked(
        self, anna: User, boris: User, site: Site, convertio: Product, placement: Placement
    ) -> None:
        other = Placement.objects.create(
            site=Site.objects.create(domain="free.example"),
            product=convertio,
            status=PlacementStatus.IN_WORK,
        )
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        response = _client(boris).post(
            PLACEMENTS,
            {
                "action": "set_status_action",
                "status": PlacementStatus.ORDERED,
                "_selected_action": [str(placement.pk), str(other.pk)],
            },
            follow=True,
        )
        placement.refresh_from_db()
        other.refresh_from_db()
        assert placement.status == PlacementStatus.IN_WORK
        assert other.status == PlacementStatus.ORDERED
        notes = [str(message) for message in response.context["messages"]]
        assert "Пропущено, сейчас правят другие: 1 — dci.com.br (Анна)." in notes


class TestSiteCard:
    def test_second_sees_view_only_but_may_note(self, anna: User, boris: User, site: Site) -> None:
        locks.take(anna, locks.site_key(site.pk))
        card = reverse("admin:sites_site_card", args=[site.pk])
        page = _client(boris).get(card, headers=PARTIAL).content.decode()
        assert 'data-seo-lock-text="Площадку правит Анна с' in page
        assert "seo-note-form" in page
        note = reverse("admin:sites_site_card_note", args=[site.pk])
        _client(boris).post(note, {"body": "Анна, эту не бери — уже договорился"}, headers=PARTIAL)
        assert SiteNote.objects.filter(site=site, body__startswith="Анна, эту").exists()

    def test_card_write_refused(self, anna: User, boris: User, site: Site) -> None:
        locks.take(anna, locks.site_key(site.pk))
        url = reverse("admin:sites_site_card_gray", args=[site.pk])
        response = _client(boris).post(url, _gray(site), headers=PARTIAL)
        assert _refused(response)
        assert not GrayScan.objects.filter(site=site).exists()

    def test_card_write_takes_the_lock(self, boris: User, site: Site) -> None:
        url = reverse("admin:sites_site_card_gray", args=[site.pk])
        _client(boris).post(url, _gray(site), headers=PARTIAL)
        assert GrayScan.objects.filter(site=site).exists()
        assert EditLock.objects.get(key=locks.site_key(site.pk)).user_id == boris.pk

    def test_card_lock_covers_every_product(
        self, anna: User, boris: User, site: Site, clideo: Product
    ) -> None:
        locks.take(anna, locks.site_key(site.pk))
        choose_product(boris, clideo)
        row = ProductSite.objects.get(site=site, product=clideo)
        url = reverse("admin:sites_productsite_decision", args=[row.pk])
        page = _client(boris).get(url, headers=PARTIAL).content.decode()
        assert "data-panel-save" not in page and "disabled" in page
        response = _client(boris).post(url, {"status": "discarded"}, headers=PARTIAL)
        assert _refused(response)
        row.refresh_from_db()
        assert row.status == WorkStatus.NEW

    def test_placements_lock_does_not_block_the_card(
        self, anna: User, boris: User, site: Site, convertio: Product
    ) -> None:
        locks.take(anna, locks.placements_key(site.pk, convertio.pk))
        url = reverse("admin:sites_site_card_gray", args=[site.pk])
        _client(boris).post(url, _gray(site), headers=PARTIAL)
        assert GrayScan.objects.filter(site=site).exists()

    def test_bulk_status_skips_locked(
        self, anna: User, boris: User, site: Site, convertio: Product
    ) -> None:
        free = Site.objects.create(domain="free.example")
        locks.take(anna, locks.site_key(site.pk))
        rows = [
            str(pk)
            for pk in ProductSiteLatest.objects.filter(product=convertio).values_list(
                "pk", flat=True
            )
        ]
        response = _client(boris).post(
            SITES + "?list=all",
            {"action": "set_status_action", "_selected_action": rows, "status": "viewed"},
            follow=True,
        )
        assert ProductSite.objects.get(site=site, product=convertio).status == WorkStatus.NEW
        assert ProductSite.objects.get(site=free, product=convertio).status == WorkStatus.VIEWED
        notes = [str(message) for message in response.context["messages"]]
        assert "Пропущено, сейчас правят другие: 1 — dci.com.br (Анна)." in notes


class TestPanelCalls:
    def test_take_state_release(self, anna: User, boris: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        taken = _client(anna).post(LOCK_URL, {"key": key, "action": "take"}).json()
        assert taken == {"mine": True, "text": ""}
        seen = _client(boris).get(LOCK_URL, {"key": key}).json()
        assert not seen["mine"] and seen["text"].startswith("Площадку правит Анна с ")
        assert seen["text"].endswith("— пока только просмотр.")
        refused = _client(boris).post(LOCK_URL, {"key": key, "action": "take"}).json()
        assert not refused["mine"] and refused["text"] == seen["text"]
        _client(anna).post(LOCK_URL, {"key": key, "action": "release"})
        assert _client(boris).get(LOCK_URL, {"key": key}).json() == {"mine": False, "text": ""}

    def test_touch_keeps_own(self, anna: User, site: Site) -> None:
        key = locks.site_key(site.pk)
        client = _client(anna)
        client.post(LOCK_URL, {"key": key, "action": "take"})
        assert client.post(LOCK_URL, {"key": key, "action": "touch"}).json()["mine"]

    def test_placements_only_of_working_product(
        self, boris: User, site: Site, clideo: Product
    ) -> None:
        key = locks.placements_key(site.pk, clideo.pk)
        response = _client(boris).post(LOCK_URL, {"key": key, "action": "take"})
        assert response.status_code == 403
        assert not EditLock.objects.filter(key=key).exists()

    def test_wrong_calls(self, boris: User, site: Site) -> None:
        client = _client(boris)
        assert client.get(LOCK_URL, {"key": "sites:1"}).status_code == 400
        key = locks.site_key(site.pk)
        assert client.get(LOCK_URL, {"key": key, "action": "take"}).status_code == 405
