"""Статус площадки по её размещениям — списки, решение, карточка (ADR-073).

Фишки размещений в колонке «статус», быстрые фильтры «статус и период» над
«Площадками» и «Размещениями», одна строка на площадку в «Размещениях»,
решение по площадке при размещениях и без них, новое размещение снимает
«Отбрасываю» и «Отказ».
"""

import datetime as dt
import re

import pytest
from django.contrib.admin.views.main import ChangeList
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements import repeat, status_chips
from apps.placements.models import Placement, PlacementStatus, PlacementStatusChange
from apps.sites import statuses
from apps.sites.admin import WORKING_LABEL, decision_choices
from apps.sites.models import (
    Product,
    ProductSite,
    ProductSiteLatest,
    Site,
    SiteStatusChange,
    WorkStatus,
)

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}
SITES = reverse("admin:sites_productsitelatest_changelist")
PLACEMENTS = reverse("admin:placements_placement_changelist")
ADD = reverse("admin:placements_placement_add")


@pytest.fixture
def convertio() -> Product:
    # Первый активный продукт — рабочий у того, кто продукт в шапке не выбирал.
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo(convertio: Product) -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _ago(days: int) -> dt.datetime:
    return timezone.now() - dt.timedelta(days=days)


def _site(domain: str, *, days: int = 0) -> Site:
    """Площадка, чьи строки «продукт × площадка» заведены `days` дней назад."""
    site = Site.objects.create(domain=domain)
    ProductSite.objects.filter(site=site).update(created_at=_ago(days))
    return site


def _deal(site: Site, product: Product, status: str, *, days: int = 0) -> Placement:
    """Размещение, чей нынешний статус поставлен `days` дней назад (история)."""
    placement = Placement.objects.create(site=site, product=product, status=status)
    PlacementStatusChange.objects.filter(placement=placement).update(changed_at=_ago(days))
    Placement.objects.filter(pk=placement.pk).update(created_at=_ago(days))
    return placement


def _decide(site: Site, product: Product, status: str, *, days: int = 0) -> ProductSite:
    """Решение по площадке, поставленное `days` дней назад (история)."""
    rows = ProductSite.objects.filter(site=site, product=product)
    rows.update(status=status)
    row = rows.get()
    SiteStatusChange.objects.filter(product_site=row).update(changed_at=_ago(days))
    return row


def _domains(rows: object) -> list[str]:
    return sorted(row.domain for row in rows)  # type: ignore[attr-defined]


def _change(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


class TestPeriod:
    NOW = timezone.make_aware(dt.datetime(2026, 10, 18, 12, 0))

    def test_presets(self) -> None:
        period = status_chips.parse_period("m3", now=self.NOW)
        assert period.key == "m3" and period.label == "3 месяца"
        assert period.start == self.NOW - dt.timedelta(days=92) and period.end is None
        assert status_chips.parse_period("y1", now=self.NOW).label == "год"

    def test_own_months(self) -> None:
        period = status_chips.parse_period(None, "2026-10", "2026-09")
        # Перепутали порядок — меняем местами; конец — начало следующего месяца.
        assert period.start == timezone.make_aware(dt.datetime(2026, 9, 1))
        assert period.end == timezone.make_aware(dt.datetime(2026, 11, 1))
        assert period.label == "09.2026 — 10.2026"
        assert status_chips.parse_period(None, "2026-09", "2026-09").label == "сентябрь 2026"
        december = status_chips.parse_period(None, None, "2026-12")
        assert december.start is None and december.label == "по 12.2026"
        assert december.end == timezone.make_aware(dt.datetime(2027, 1, 1))
        assert status_chips.parse_period(None, "2025-01", "").label == "с 01.2025"

    def test_garbage_is_all_time(self) -> None:
        period = status_chips.parse_period("week", "2026-13", "осень")
        assert not period.active and period.label == "Всё время"
        assert period.holds(None)


class TestFilters:
    @pytest.fixture
    def world(self, convertio: Product, clideo: Product) -> None:
        fresh = _site("fresh.com", days=5)
        _site("old-new.com", days=200)
        viewed = _site("viewed.com", days=300)
        _decide(viewed, convertio, WorkStatus.VIEWED, days=10)
        recent = _site("recent.com", days=300)
        _deal(recent, convertio, PlacementStatus.PLACED, days=3)
        old_deal = _site("old-deal.com", days=500)
        _deal(old_deal, convertio, PlacementStatus.PLACED, days=400)
        stopped = _site("stopped.com", days=500)
        _deal(stopped, convertio, PlacementStatus.PLACED, days=400)
        _decide(stopped, convertio, WorkStatus.DISCARDED, days=2)
        # Свой статус при размещениях не считается: его ведут размещения.
        legacy = _site("legacy.com", days=500)
        _deal(legacy, convertio, PlacementStatus.IN_WORK, days=1)
        _decide(legacy, convertio, WorkStatus.VIEWED, days=1)
        # Размещение другого продукта рамку не трогает.
        _deal(fresh, clideo, PlacementStatus.PLACED, days=1)

    def _sites(self, convertio: Product, status: str | None, period: str = "") -> list[str]:
        rows = ProductSiteLatest.objects.filter(product=convertio)
        found = status_chips.filter_sites(rows, status, status_chips.parse_period(period))
        return _domains(found)

    @pytest.mark.usefixtures("world")
    def test_sites_by_status_and_period(self, convertio: Product) -> None:
        assert self._sites(convertio, WorkStatus.NEW, "m1") == ["fresh.com"]
        assert self._sites(convertio, WorkStatus.NEW) == ["fresh.com", "old-new.com"]
        assert self._sites(convertio, WorkStatus.PLACED, "m1") == ["recent.com"]
        assert self._sites(convertio, WorkStatus.PLACED) == [
            "old-deal.com",
            "recent.com",
            "stopped.com",
        ]
        assert self._sites(convertio, WorkStatus.DISCARDED, "m1") == ["stopped.com"]
        assert self._sites(convertio, WorkStatus.VIEWED) == ["viewed.com"]
        assert self._sites(convertio, WorkStatus.IN_WORK) == ["legacy.com"]

    @pytest.mark.usefixtures("world")
    def test_any_status_in_period(self, convertio: Product) -> None:
        assert self._sites(convertio, None, "m1") == [
            "fresh.com",
            "legacy.com",
            "recent.com",
            "stopped.com",
            "viewed.com",
        ]

    @pytest.mark.usefixtures("world")
    def test_placements_by_status_and_period(self, convertio: Product) -> None:
        rows = Placement.objects.filter(product=convertio)
        month = status_chips.parse_period("m1")
        found = status_chips.filter_placements(rows, WorkStatus.PLACED, month)
        assert [p.site.domain for p in found] == ["recent.com"]
        assert status_chips.filter_placements(rows, None, month).count() == 2

    @pytest.mark.usefixtures("world")
    def test_sites_list_bar(self, admin_client: Client) -> None:
        response = admin_client.get(
            SITES, {"list": "all", "status__exact": "placed", "period": "m1"}
        )
        assert [row.domain for row in response.context["cl"].result_list] == ["recent.com"]
        page = response.content.decode()
        assert '<a class="seo-stbar-chip seo-stbar-on"' in page
        assert "Период: месяц ▾" in page
        # «Свой период» отправляет и остальные параметры списка.
        assert '<input type="hidden" name="list" value="all">' in page
        assert '<input type="hidden" name="status__exact" value="placed">' in page
        own = admin_client.get(
            SITES, {"list": "all", "period_from": "2026-01", "period_to": "2026-12"}
        ).content.decode()
        assert "Период: 01.2026 — 12.2026 ▾" in own
        assert 'name="period_from" value="2026-01"' in own


class TestCells:
    def test_closed_by_period_live_always(self, convertio: Product) -> None:
        site = _site("donor.com")
        first = _deal(site, convertio, PlacementStatus.PLACED, days=400)
        second = _deal(site, convertio, PlacementStatus.IN_WORK, days=1)
        month = status_chips.cells([site.pk], convertio.pk, status_chips.parse_period("m1"))
        assert [chip.label for chip in month[site.pk].chips] == ["В работе"]
        every = status_chips.cells([site.pk], convertio.pk, status_chips.Period())
        chips = every[site.pk].chips
        assert [(chip.label, chip.tone, chip.url) for chip in chips] == [
            ("Размещено", "ok", _change(first)),
            ("В работе", "live", _change(second)),
        ]
        assert chips[0].title.startswith("№1 · ") and chips[1].title.startswith("№2 · ")
        assert every[site.pk].placements == 2

    def test_many_closed_are_grouped(self, convertio: Product) -> None:
        site = _site("donor.com")
        placed = [_deal(site, convertio, PlacementStatus.PLACED, days=30 * n) for n in (5, 4, 3)]
        _deal(site, convertio, PlacementStatus.IN_WORK)
        chips = status_chips.cells([site.pk], convertio.pk, status_chips.Period())[site.pk].chips
        assert [(chip.label, chip.count) for chip in chips] == [("Размещено", 3), ("В работе", 1)]
        # Группа открывает последнее из своих, в подсказке — все номера.
        assert chips[0].url == _change(placed[-1])
        assert chips[0].title.count("№") == 3
        html = status_chips.cell_html(
            status_chips.cells([site.pk], convertio.pk, status_chips.Period())[site.pk]
        )
        assert "Размещено ×3" in html

    def test_stop_after_placements(self, convertio: Product) -> None:
        site = _site("donor.com")
        _deal(site, convertio, PlacementStatus.PLACED, days=400)
        row = _decide(site, convertio, WorkStatus.REJECTED, days=2)
        # Период «месяц»: размещение старше, а «больше не работаем» видно всегда.
        cell = status_chips.cells([site.pk], convertio.pk, status_chips.parse_period("m1"))[site.pk]
        [stop] = cell.chips
        assert (stop.label, stop.tone) == ("Отказ", "no")
        assert stop.panel == reverse("admin:sites_productsite_decision", args=[row.pk])

    def test_nothing_in_period(self, convertio: Product) -> None:
        site = _site("donor.com")
        placement = _deal(site, convertio, PlacementStatus.PLACED, days=400)
        cell = status_chips.cells([site.pk], convertio.pk, status_chips.parse_period("m1"))[site.pk]
        assert cell.chips == [] and cell.hidden_url == _change(placement)
        assert "нет за период" in status_chips.cell_html(cell)

    def test_without_placements_decision_date(self, convertio: Product) -> None:
        site = _site("donor.com", days=40)
        cell = status_chips.cells([site.pk], convertio.pk, status_chips.Period())[site.pk]
        assert cell.placements == 0 and cell.chips == []
        assert cell.decided is not None and cell.decided < _ago(39)
        _decide(site, convertio, WorkStatus.VIEWED, days=3)
        cell = status_chips.cells([site.pk], convertio.pk, status_chips.Period())[site.pk]
        assert cell.decided is not None and _ago(4) < cell.decided < _ago(2)

    def test_dates_same_in_python_and_sql(self, convertio: Product) -> None:
        site = _site("donor.com")
        ordered = Placement.objects.create(site=site, product=convertio)
        ordered.status = PlacementStatus.ORDERED
        ordered.save()
        PlacementStatusChange.objects.filter(placement=ordered).delete()
        # Истории нет — дата заявки; нет и её — когда завели.
        dates = repeat.status_dates([ordered])
        ordered.refresh_from_db()
        assert dates[ordered.pk] == ordered.ordered_at


class TestLists:
    def test_sites_list_cells(self, admin_client: Client, convertio: Product) -> None:
        site = _site("donor.com")
        first = _deal(site, convertio, PlacementStatus.PLACED, days=40)
        second = _deal(site, convertio, PlacementStatus.ORDERED, days=2)
        _site("fresh.com", days=3)
        page = admin_client.get(SITES, {"list": "all"}).content.decode()
        assert f'href="{_change(first)}" data-panel="{_change(first)}"' in page
        # Домен открывает карточку площадки, размещения — фишки (19.10.2026).
        card = reverse("admin:sites_site_card", args=[site.pk])
        assert f'href="{card}" data-panel title="Карточка площадки">donor.com</a>' in page
        assert f'href="{_change(second)}" data-panel="{_change(second)}"' in page
        # Без размещений — решение с датой.
        assert re.search(r'data-seo-field="status">Новая</a><span class="seo-decided">', page)

    def test_placements_one_row_per_site(self, admin_client: Client, convertio: Product) -> None:
        site = _site("donor.com")
        first = _deal(site, convertio, PlacementStatus.PLACED, days=40)
        second = _deal(site, convertio, PlacementStatus.IN_WORK)
        other = _deal(_site("other.com"), convertio, PlacementStatus.PLACED)
        changelist: ChangeList = admin_client.get(PLACEMENTS).context["cl"]
        assert sorted(row.pk for row in changelist.result_list) == [second.pk, other.pk]
        assert changelist.result_count == 2 and changelist.full_result_count == 2
        # Фильтр выбирает своё размещение площадки.
        placed = admin_client.get(PLACEMENTS, {"status__exact": "placed"}).context["cl"]
        assert sorted(row.pk for row in placed.result_list) == [first.pk, other.pk]

    def test_actions_take_marked_or_rows(self, admin_client: Client, convertio: Product) -> None:
        site = _site("donor.com")
        first = _deal(site, convertio, PlacementStatus.ORDERED, days=40)
        second = _deal(site, convertio, PlacementStatus.IN_WORK)
        action = {"action": "set_status_action", "status": "writing", "index": "0"}
        # Отмеченное — как есть, даже если строка списка показывает другое.
        admin_client.post(PLACEMENTS, {**action, "_selected_action": [str(first.pk)]})
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.status, second.status) == ("writing", "in_work")
        # «Выбрать все» — строки списка: по одному размещению на площадку.
        Placement.objects.filter(pk=first.pk).update(status="placed")
        admin_client.post(
            PLACEMENTS,
            {**action, "select_across": "1", "_selected_action": [str(second.pk)]},
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.status, second.status) == ("placed", "writing")


class TestDecision:
    def test_choices(self, convertio: Product) -> None:
        own = ["new", "viewed", "discarded", "rejected", "blacklisted"]
        assert [value for value, _ in decision_choices("new", [])] == own
        # Старые данные: статус работы без размещений остаётся кнопкой.
        assert ("in_work", "В работе") in decision_choices("in_work", [])
        site = _site("donor.com")
        placed = _deal(site, convertio, PlacementStatus.PLACED)
        assert decision_choices("placed", [placed])[0] == ("placed", WORKING_LABEL)
        # «Работаем» после «больше не работаем» — статус по размещениям.
        live = _deal(site, convertio, PlacementStatus.ORDERED)
        assert decision_choices("discarded", [placed])[0][0] == "placed"
        assert decision_choices("discarded", [placed, live])[0][0] == "ordered"

    def test_card_shows_placements(
        self, admin_client: Client, convertio: Product, clideo: Product
    ) -> None:
        site = _site("donor.com")
        ours = _deal(site, convertio, PlacementStatus.ORDERED)
        theirs = _deal(site, clideo, PlacementStatus.PLACED)
        page = admin_client.get(
            reverse("admin:sites_site_card", args=[site.pk]), headers=PARTIAL
        ).content.decode()
        assert f'href="{_change(ours)}" data-panel="{_change(ours)}"' in page
        assert "Больше не работаем…" in page
        # Чужой продукт — фишками, только просмотр.
        assert f'href="{_change(theirs)}" data-panel="{_change(theirs)}"' in page
        assert "· только просмотр" in page

    def test_card_without_placements(self, admin_client: Client, convertio: Product) -> None:
        site = _site("donor.com")
        page = admin_client.get(
            reverse("admin:sites_site_card", args=[site.pk]), headers=PARTIAL
        ).content.decode()
        assert ">Сменить статус</a>" in page and "seo-st-chips" not in page


class TestReopen:
    def test_take_clears_discarded(self, convertio: Product) -> None:
        site = _site("donor.com")
        _decide(site, convertio, WorkStatus.DISCARDED)
        repeat.take([site.pk], convertio.pk)
        row = ProductSite.objects.get(site=site, product=convertio)
        assert row.status == WorkStatus.IN_WORK
        change = SiteStatusChange.objects.filter(product_site=row).order_by("-pk").first()
        assert change is not None
        assert (change.from_status, change.placement_id) == (
            "discarded",
            Placement.objects.get().pk,
        )

    def test_form_clears_rejected(self, admin_client: Client, convertio: Product) -> None:
        site = _site("donor.com")
        _decide(site, convertio, WorkStatus.REJECTED)
        data = {
            "site": str(site.pk),
            "product": str(convertio.pk),
            "status": "in_work",
            "currency": "EUR",
            "links-TOTAL_FORMS": "0",
            "links-INITIAL_FORMS": "0",
            "links-MIN_NUM_FORMS": "0",
            "links-MAX_NUM_FORMS": "1000",
        }
        response = admin_client.post(ADD, data)
        assert response.status_code == 302
        assert ProductSite.objects.get(site=site, product=convertio).status == WorkStatus.IN_WORK

    def test_blacklist_stays(self, convertio: Product) -> None:
        site = _site("donor.com")
        _decide(site, convertio, WorkStatus.BLACKLISTED)
        placement = Placement.objects.create(site=site, product=convertio)
        assert not statuses.reopen(site.pk, convertio.pk, placement_id=placement.pk)
        assert ProductSite.objects.get(site=site, product=convertio).status == "blacklisted"

    def test_import_rules_unchanged(self, convertio: Product) -> None:
        # Система (импорт таблицы) заводит размещение «В работе» — отказ остаётся.
        site = _site("donor.com")
        _decide(site, convertio, WorkStatus.DISCARDED)
        Placement.objects.create(site=site, product=convertio)
        assert ProductSite.objects.get(site=site, product=convertio).status == "discarded"
