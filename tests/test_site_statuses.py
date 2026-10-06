"""Статусы площадки (E1-12, ADR-047): порядок и смена по размещениям.

Порядок — в окне «Решение по площадке», в фильтре «Статус» и в сортировке по
колонке «статус» в «Площадках». Смена по размещениям — `apps.sites.statuses`
и `Placement.save()`: только вперёд по лесенке, новый факт сильнее прежнего
отказа и аудита, «Чёрный список» не трогается никогда. Как то же правило
работает в импорте таблицы — `test_import_apply.py`.
"""

import datetime as dt
import re

import pytest
from django.contrib.admin.views.main import ORDER_VAR, ChangeList
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement, PlacementStatus
from apps.sites import statuses
from apps.sites.models import Product, ProductSite, Site, SiteStatus

pytestmark = pytest.mark.django_db

# Слова, о которых договорились с пользователем 06.10.2026 (ADR-062): один
# словарь на площадку и размещение, в этом порядке.
AGREED = [
    "Новая",
    "Просмотрено",
    "В работе",
    "Заявка отправлена",
    "Написание статьи",
    "Размещено",
    "Отбрасываю",
    "Отказ",
    "Чёрный список",
]
SITES_URL = reverse("admin:sites_productsitelatest_changelist")
PARTIAL = {"X-Seo-Partial": "1"}


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def site(convertio: Product, clideo: Product) -> Site:
    return Site.objects.create(domain="example.com")


def _row(site: Site, product: Product) -> ProductSite:
    return ProductSite.objects.get(site=site, product=product)


def _set(site: Site, product: Product, status: SiteStatus, reason: str | None = None) -> None:
    ProductSite.objects.filter(site=site, product=product).update(status=status, comment=reason)


def _place(site: Site, product: Product, status: PlacementStatus) -> Placement:
    return Placement.objects.create(site=site, product=product, status=status)


class TestOrder:
    def test_labels(self) -> None:
        assert [status.label for status in SiteStatus] == AGREED

    def test_decision_panel(self, admin_client: Client, site: Site, convertio: Product) -> None:
        url = reverse("admin:sites_productsite_decision", args=[_row(site, convertio).pk])
        page = admin_client.get(url, headers=PARTIAL).content.decode()
        buttons = re.search(r'<div class="seo-choice".*?</div></div>', page, re.S)
        assert buttons is not None
        assert re.findall(r"<span>([^<]*)</span>", buttons.group()) == AGREED
        # Два ряда: путь площадки в работу и отказы.
        rows = re.findall(r'<div class="seo-choice-row">(.*?)</div>', buttons.group(), re.S)
        assert [re.findall(r"<span>([^<]*)</span>", row) for row in rows] == [
            AGREED[:2],
            AGREED[2:6],
            AGREED[6:],
        ]

    def test_status_filter(self, admin_client: Client, site: Site) -> None:
        response = admin_client.get(SITES_URL, {"list": "all"})
        changelist: ChangeList = response.context["cl"]
        [spec] = [s for s in changelist.filter_specs if getattr(s, "field_path", "") == "status"]
        choices = [str(choice["display"]) for choice in spec.choices(changelist)]
        assert choices[1:] == AGREED

    def test_sort_by_status_column(self, admin_client: Client, convertio: Product) -> None:
        for index, status in enumerate(reversed(SiteStatus)):
            _set(Site.objects.create(domain=f"s{index}.com"), convertio, status)
        changelist: ChangeList = admin_client.get(SITES_URL, {"list": "all"}).context["cl"]
        column = list(changelist.list_display).index("status_link")
        response = admin_client.get(SITES_URL, {"list": "all", ORDER_VAR: str(column)})
        rows = response.context["cl"].result_list
        assert [SiteStatus(row.status).label for row in rows] == AGREED


class TestRules:
    def test_ladder_forward_only(self) -> None:
        assert statuses.replaceable(SiteStatus.NEW) == ()
        assert statuses.replaceable(SiteStatus.IN_WORK) == (SiteStatus.NEW, SiteStatus.VIEWED)
        assert statuses.replaceable(SiteStatus.ORDERED) == (
            SiteStatus.NEW,
            SiteStatus.VIEWED,
            SiteStatus.IN_WORK,
        )

    def test_new_fact_overrides_refusals_and_audit_not_blacklist(self) -> None:
        for target in (SiteStatus.ORDERED, SiteStatus.PLACED):
            replaced = statuses.replaceable(target, fact=True)
            assert {SiteStatus.DISCARDED, SiteStatus.REJECTED, SiteStatus.IN_WORK} <= set(replaced)
            assert SiteStatus.BLACKLISTED not in replaced
        assert SiteStatus.PLACED not in statuses.replaceable(SiteStatus.ORDERED, fact=True)

    def test_decision_without_fact_only_where_undecided(self) -> None:
        undecided = (SiteStatus.NEW, SiteStatus.VIEWED)
        assert statuses.replaceable(SiteStatus.DISCARDED) == undecided
        # Запланированное размещение — не факт: прежний отказ «Одобрена» не перекрывает.
        assert statuses.replaceable(SiteStatus.IN_WORK, fact=True) == undecided


class TestBulkStatus:
    """Действие «Поставить статус…» — статус отмеченным строкам (ADR-062)."""

    def test_sites_list_sets_status(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        other = Site.objects.create(domain="second.com")
        rows = [_row(site, convertio).pk, _row(other, convertio).pk]
        response = admin_client.post(
            SITES_URL,
            {
                "action": "set_status_action",
                "status": SiteStatus.IN_WORK,
                "_selected_action": [str(pk) for pk in rows],
            },
            follow=True,
        )
        assert "Статус «В работе» поставлен: 2" in response.content.decode()
        assert _row(site, convertio).status == SiteStatus.IN_WORK
        assert _row(other, convertio).status == SiteStatus.IN_WORK

    def test_without_status_nothing_happens(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        response = admin_client.post(
            SITES_URL,
            {
                "action": "set_status_action",
                "status": "",
                "_selected_action": [str(_row(site, convertio).pk)],
            },
            follow=True,
        )
        assert "Выберите статус рядом с действием" in response.content.decode()
        assert _row(site, convertio).status == SiteStatus.NEW

    def test_placements_list_sets_status_and_site_follows(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        placement = Placement.objects.create(site=site, product=convertio)
        response = admin_client.post(
            reverse("admin:placements_placement_changelist"),
            {
                "action": "set_status_action",
                "status": SiteStatus.ORDERED,
                "_selected_action": [str(placement.pk)],
            },
            follow=True,
        )
        assert "Статус «Заявка отправлена» поставлен: 1" in response.content.decode()
        placement.refresh_from_db()
        assert placement.status == SiteStatus.ORDERED
        # Площадка идёт за размещением: словарь общий.
        assert _row(site, convertio).status == SiteStatus.ORDERED


class TestByPlacement:
    @pytest.mark.parametrize("before", [SiteStatus.NEW, SiteStatus.VIEWED, SiteStatus.IN_WORK])
    def test_order_moves_forward(
        self, site: Site, convertio: Product, clideo: Product, before: SiteStatus
    ) -> None:
        _set(site, convertio, before)
        _place(site, convertio, PlacementStatus.ORDERED)
        assert _row(site, convertio).status == SiteStatus.ORDERED
        # Площадка у другого продукта — своё решение.
        assert _row(site, clideo).status == SiteStatus.NEW

    def test_placement_in_work_moves_the_site(self, site: Site, convertio: Product) -> None:
        # Словарь общий (ADR-062): размещение «В работе» ставит площадке тот же статус.
        _place(site, convertio, PlacementStatus.IN_WORK)
        assert _row(site, convertio).status == SiteStatus.IN_WORK

    def test_writing_moves_the_site_to_writing(self, site: Site, convertio: Product) -> None:
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        assert _row(site, convertio).status == SiteStatus.ORDERED
        placement.status = PlacementStatus.WRITING
        placement.save()
        assert _row(site, convertio).status == SiteStatus.WRITING

    def test_publication_after_order(self, site: Site, convertio: Product) -> None:
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        placement.status = PlacementStatus.PLACED
        placement.save()
        assert _row(site, convertio).status == SiteStatus.PLACED

    def test_never_back(self, site: Site, convertio: Product) -> None:
        published = _place(site, convertio, PlacementStatus.PLACED)
        _place(site, convertio, PlacementStatus.ORDERED)
        assert _row(site, convertio).status == SiteStatus.PLACED
        published.status = PlacementStatus.REJECTED
        published.save()
        assert _row(site, convertio).status == SiteStatus.PLACED

    @pytest.mark.parametrize(
        "before", [SiteStatus.DISCARDED, SiteStatus.REJECTED, SiteStatus.IN_WORK]
    )
    def test_new_order_overrides_earlier_decision(
        self, site: Site, convertio: Product, before: SiteStatus
    ) -> None:
        _set(site, convertio, before, "Nofollow, отбрасываем")
        _place(site, convertio, PlacementStatus.ORDERED)
        row = _row(site, convertio)
        assert row.status == SiteStatus.ORDERED
        assert row.comment == "Nofollow, отбрасываем"

    def test_blacklist_is_never_touched(self, site: Site, convertio: Product) -> None:
        _set(site, convertio, SiteStatus.BLACKLISTED)
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        placement.status = PlacementStatus.PLACED
        placement.save()
        assert _row(site, convertio).status == SiteStatus.BLACKLISTED

    @pytest.mark.parametrize("status", [PlacementStatus.REJECTED, PlacementStatus.REJECTED])
    def test_rejected_or_cancelled_placement_moves_nothing(
        self, site: Site, convertio: Product, status: PlacementStatus
    ) -> None:
        _set(site, convertio, SiteStatus.IN_WORK)
        placement = _place(site, convertio, PlacementStatus.IN_WORK)
        placement.status = status
        placement.save()
        assert _row(site, convertio).status == SiteStatus.IN_WORK

    def test_edit_without_status_change_keeps_manual_decision(
        self, site: Site, convertio: Product
    ) -> None:
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        # Площадка отказала уже после заявки — человек ставит это руками.
        _set(site, convertio, SiteStatus.REJECTED, "Отказали, без объяснения")
        placement.comment = "ждали неделю"
        placement.save()
        placement.save(update_fields=["comment"])
        Placement.objects.get(pk=placement.pk).save()
        assert _row(site, convertio).status == SiteStatus.REJECTED

    def test_undecided_mark_and_time(self, site: Site, convertio: Product) -> None:
        old = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        ProductSite.objects.filter(site=site, product=convertio).update(
            imported_undecided=True, updated_at=old
        )
        _place(site, convertio, PlacementStatus.ORDERED)
        row = _row(site, convertio)
        assert row.imported_undecided is False
        assert row.updated_at > old

    def test_placement_moved_to_other_site(self, site: Site, convertio: Product) -> None:
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        other = Site.objects.create(domain="other.com")
        placement.site = other
        placement.save()
        assert _row(other, convertio).status == SiteStatus.ORDERED

    def test_from_admin_form(self, admin_client: Client, site: Site, convertio: Product) -> None:
        placement = _place(site, convertio, PlacementStatus.ORDERED)
        url = reverse("admin:placements_placement_change", args=[placement.pk])
        data = {
            "site": str(site.pk),
            "product": str(convertio.pk),
            "status": PlacementStatus.PLACED,
            "currency": "EUR",
            "links-TOTAL_FORMS": "0",
            "links-INITIAL_FORMS": "0",
            "links-MIN_NUM_FORMS": "0",
            "links-MAX_NUM_FORMS": "1000",
        }
        assert admin_client.post(url, data).status_code == 302
        assert _row(site, convertio).status == SiteStatus.PLACED
