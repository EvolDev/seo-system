"""Проверка индексации статьи размещения (E2-03, ADR-042)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.content.domain_settings import set_product_setting
from apps.content.models import DomainSetting
from apps.integrations.serp import SerpPage, SerpResult
from apps.integrations.serp import client as serp_client
from apps.integrations.serp.types import ProviderAnswer
from apps.observability.models import ApiUsage, Check, CheckStatus, TaskRun, TaskStatus
from apps.placements import indexation, tasks
from apps.placements.indexation import (
    find_position,
    normalize_url,
    same_page,
    search_query,
)
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site
from config.run_id import bind_run_id

pytestmark = pytest.mark.django_db

URL = "https://example.com/blog/how-to-convert/"
SITE_QUERY = "site:example.com/blog/how-to-convert/"
SCHEDULE = {
    "enabled": True,
    "first_check_days": 3,
    "retry_days": 1,
    "recheck_days": 30,
    "alert_after_days": 30,
}


def _at(day: int, hour: int = 6, minute: int = 0) -> datetime:
    """Момент 1–31 октября 2026 года в TIME_ZONE."""
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.get_current_timezone())


def _page(*urls: str) -> SerpPage:
    results = tuple(
        SerpResult(position, url, "example.com", "t", "") for position, url in enumerate(urls, 1)
    )
    return SerpPage(
        query="q",
        depth=10,
        country="us",
        results=results,
        total_estimate=None,
        fetched_at=timezone.now(),
    )


class FakeSearch:
    """Выдача без сети: на запрос из `by_query` — его адреса, на любой другой — `urls`."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.by_query: dict[str, list[str]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, query: str, **kwargs: Any) -> SerpPage:
        self.calls.append((query, kwargs))
        return _page(*self.by_query.get(query, self.urls))


@pytest.fixture
def serp(monkeypatch: pytest.MonkeyPatch) -> FakeSearch:
    fake = FakeSearch()
    monkeypatch.setattr(indexation, "search", fake)
    return fake


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ограничение скорости ходит в Redis приложения и тормозило бы eager-задачи;
    # что оно задано — отдельный тест.
    monkeypatch.setattr(tasks.check_indexation, "throttle", None)


@pytest.fixture
def product() -> Product:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    set_product_setting(product.pk, "INDEXATION_SCHEDULE", SCHEDULE)
    return product


@pytest.fixture
def make_placement(product: Product) -> Callable[..., Placement]:
    counter = iter(range(1, 1000))

    def make(**fields: Any) -> Placement:
        site = Site.objects.create(domain=f"site{next(counter)}.com")
        defaults: dict[str, Any] = {
            "site": site,
            "product": product,
            "status": PlacementStatus.PUBLISHED,
            "article_url": URL,
            "published_at": _at(1, 12),
        }
        return Placement.objects.create(**{**defaults, **fields})

    return make


@pytest.fixture
def placement(make_placement: Callable[..., Placement]) -> Placement:
    return make_placement()


def _checks(placement: Placement) -> list[Check]:
    return list(
        Check.objects.filter(entity_id=placement.pk, check_type="indexation").order_by("id")
    )


class TestUrls:
    @pytest.mark.parametrize(
        "other",
        [
            "https://example.com/blog/how-to-convert/",
            "http://example.com/blog/how-to-convert",
            "https://www.example.com/blog/how-to-convert/",
            "https://EXAMPLE.com/blog/how-to-convert/#intro",
            "https://example.com:443/blog/how-to-convert/",
            "example.com/blog/how-to-convert",
        ],
    )
    def test_same_article(self, other: str) -> None:
        assert normalize_url(other) == normalize_url(URL)

    @pytest.mark.parametrize(
        "other",
        [
            "https://example.com/blog/",
            "https://example.com/blog/how-to-convert/page/2/",
            "https://example.com/Blog/how-to-convert/",
            "https://blog.example.com/blog/how-to-convert/",
        ],
    )
    def test_other_page(self, other: str) -> None:
        assert normalize_url(other) != normalize_url(URL)
        assert not same_page(URL, other)

    @pytest.mark.parametrize(
        "found",
        [
            # Так Google показал статью artistpush.me (Shopify) 01.10.2026.
            "https://example.com/blog/how-to-convert?srsltid=AU7gw4XMInnpCDJLV9tIjD4KaYT5",
            "https://example.com/blog/how-to-convert/?utm_source=x&UTM_medium=y",
            "https://example.com/blog/how-to-convert/?p=1",
        ],
    )
    def test_article_without_query_ignores_query_in_results(self, found: str) -> None:
        assert same_page(URL, found)

    def test_article_with_query_compares_it_without_tracking(self) -> None:
        article = "https://x.com/index.php?p=12&lang=en"
        assert same_page(article, "https://x.com/index.php?lang=en&p=12&srsltid=abc&gclid=1")
        assert not same_page(article, "https://x.com/index.php?p=13&lang=en")
        assert not same_page(article, "https://x.com/index.php")

    def test_percent_encoding(self) -> None:
        assert normalize_url("https://x.com/caf%C3%A9") == normalize_url("https://x.com/café")

    def test_query(self) -> None:
        assert search_query(URL) == "site:example.com/blog/how-to-convert/"
        assert search_query(" http://x.com/a#top ") == "site:x.com/a"

    def test_position_only_for_exact_url(self) -> None:
        page = _page("https://example.com/blog/", "https://www.example.com/blog/how-to-convert")
        assert find_position(URL, page) == 2
        assert find_position(URL, _page("https://example.com/blog/")) is None


class TestCheckPlacement:
    def test_indexed(self, placement: Placement, serp: FakeSearch) -> None:
        serp.urls = ["https://example.com/blog/how-to-convert/"]
        run_id = uuid4()
        with bind_run_id(run_id):
            outcome = indexation.check_placement(placement, manual=False, now=_at(4, 6, 5))
        assert outcome.indexed
        check = outcome.check
        assert (check.entity_type, check.check_type, check.status) == (
            "placement",
            "indexation",
            CheckStatus.OK,
        )
        assert check.result == {
            "url": URL,
            "manual": False,
            "queries": ["site:example.com/blog/how-to-convert/"],
            "found_by": "site",
            "position": 1,
        }
        assert check.run_id == run_id
        # В индексе — следующая через 30 дней, с полуночи.
        assert check.next_check_at == _at(4, 0) + timedelta(days=30)
        placement.refresh_from_db()
        assert placement.is_indexed is True
        assert placement.indexed_checked_at == _at(4, 6, 5)

    def test_not_indexed(self, placement: Placement, serp: FakeSearch) -> None:
        serp.urls = ["https://example.com/blog/"]
        outcome = indexation.check_placement(placement, manual=True, now=_at(4, 15))
        assert not outcome.indexed
        assert not outcome.alert
        assert outcome.check.status == CheckStatus.FAILED
        assert outcome.check.result is not None
        assert outcome.check.result["manual"] is True
        assert outcome.check.result["failing_days"] == 3
        # Что было в выдаче вместо статьи — для разбора.
        assert outcome.check.result["seen"] == ["https://example.com/blog/"]
        # Не в индексе — завтра с полуночи: утренний запуск её застанет.
        assert outcome.check.next_check_at == _at(5, 0)
        placement.refresh_from_db()
        assert placement.is_indexed is False

    def test_site_found_one_query(self, placement: Placement, serp: FakeSearch) -> None:
        serp.urls = [URL]
        indexation.check_placement(placement, manual=False)
        assert serp.calls == [(SITE_QUERY, {"depth": 10, "fresh": True})]

    def test_not_found_by_site_searches_url(self, placement: Placement, serp: FakeSearch) -> None:
        """`site:` статью не показал, обычный поиск по адресу нашёл — в индексе (ADR-042)."""
        serp.by_query = {URL: ["https://example.com/", URL]}
        outcome = indexation.check_placement(placement, manual=False)
        assert serp.calls == [
            (SITE_QUERY, {"depth": 10, "fresh": True}),
            (URL, {"depth": 10, "fresh": True}),
        ]
        assert outcome.indexed
        assert outcome.check.result is not None
        assert outcome.check.result["found_by"] == "url"
        assert outcome.check.result["position"] == 2
        assert outcome.check.result["queries"] == ["site:example.com/blog/how-to-convert/", URL]

    def test_not_found_by_both(self, placement: Placement, serp: FakeSearch) -> None:
        serp.by_query = {
            "site:example.com/blog/how-to-convert/": ["https://example.com/blog/"],
            URL: ["https://other.com/", "https://example.com/blog/"],
        }
        outcome = indexation.check_placement(placement, manual=False)
        assert not outcome.indexed
        assert outcome.check.result is not None
        assert "found_by" not in outcome.check.result
        # Что было в обеих выдачах, без повторов.
        assert outcome.check.result["seen"] == ["https://example.com/blog/", "https://other.com/"]

    def test_cost_is_kept_when_recording_fails(
        self, placement: Placement, monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        """Поиск — вне транзакции записи: упала запись — расход всё равно в `api_usage`."""
        settings.SERP_DAILY_BUDGET_CENTS = 500

        class Provider:
            name = "serper"

            def fetch(self, query: str, depth: int, country: str) -> ProviderAnswer:
                return ProviderAnswer(_page(), "search", 1, Decimal("0.1"))

        monkeypatch.setitem(serp_client.PROVIDERS, "serper", Provider)

        def broken(*args: Any, **kwargs: Any) -> Check:
            raise RuntimeError("запись упала")

        monkeypatch.setattr(Check.objects, "create", broken)
        with pytest.raises(RuntimeError):
            indexation.check_placement(placement, manual=False)
        # Оба запроса (site: и по адресу) оплачены и записаны.
        assert ApiUsage.objects.count() == 2
        placement.refresh_from_db()
        assert placement.is_indexed is None


class TestSchedule:
    def _due(self, now: datetime) -> list[int]:
        return indexation.due_placement_ids(now)

    def test_first_check_three_days_after_publication(self, placement: Placement) -> None:
        # Опубликовано 1-го в 12:00 → первая проверка 4-го с полуночи.
        assert self._due(_at(3, 23, 59)) == []
        assert self._due(_at(4, 0)) == [placement.pk]
        assert self._due(_at(4, 6)) == [placement.pk]

    def test_without_publication_date_checked_at_once(
        self, make_placement: Callable[..., Placement]
    ) -> None:
        placement = make_placement(published_at=None)
        assert self._due(_at(1)) == [placement.pk]

    def test_daily_until_indexed_then_monthly(self, placement: Placement, serp: FakeSearch) -> None:
        indexation.check_placement(placement, manual=False, now=_at(4, 6, 5))
        # Вчерашняя проверка в 06:05 — сегодня в 06:00 уже пора.
        assert self._due(_at(5, 0)) == [placement.pk]
        assert self._due(_at(4, 23)) == []
        serp.urls = [URL]
        indexation.check_placement(placement, manual=False, now=_at(5, 6, 5))
        # В индексе — месячный цикл: следующий день не берёт.
        assert self._due(_at(6, 6)) == []
        assert self._due(_at(5, 0) + timedelta(days=29, hours=23)) == []
        assert self._due(_at(5, 0) + timedelta(days=30)) == [placement.pk]

    def test_dropped_out_returns_to_daily(self, placement: Placement, serp: FakeSearch) -> None:
        serp.urls = [URL]
        indexation.check_placement(placement, manual=False, now=_at(4, 6))
        serp.urls = []
        indexation.check_placement(placement, manual=False, now=_at(4, 6) + timedelta(days=30))
        assert self._due(_at(4, 6) + timedelta(days=31)) == [placement.pk]

    def test_changed_url_is_checked_again(self, placement: Placement, serp: FakeSearch) -> None:
        serp.urls = [URL]
        indexation.check_placement(placement, manual=False, now=_at(4, 6))
        assert self._due(_at(5, 6)) == []
        placement.article_url = "https://example.com/blog/new-address/"
        placement.save()
        assert self._due(_at(5, 6)) == [placement.pk]

    def test_human_check_without_url_keeps_its_date(self, placement: Placement) -> None:
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.OK,
            performed_by="human",
            checked_at=_at(2),
            next_check_at=_at(2) + timedelta(days=30),
        )
        assert self._due(_at(10)) == []
        assert self._due(_at(2) + timedelta(days=30)) == [placement.pk]

    @pytest.mark.parametrize(
        "fields",
        [
            {"status": PlacementStatus.ORDERED},
            {"status": PlacementStatus.CANCELLED},
            {"article_url": None},
            {"article_url": ""},
            {"skip_checks": True},
        ],
    )
    def test_not_scheduled(
        self, make_placement: Callable[..., Placement], fields: dict[str, Any]
    ) -> None:
        make_placement(**fields)
        assert self._due(_at(10)) == []

    def test_switched_off(self, placement: Placement, product: Product) -> None:
        set_product_setting(product.pk, "INDEXATION_SCHEDULE", {**SCHEDULE, "enabled": False})
        assert self._due(_at(10)) == []

    def test_general_schedule_is_off_by_default(
        self, make_placement: Callable[..., Placement], product: Product
    ) -> None:
        DomainSetting.objects.filter(product=product).delete()
        make_placement()
        assert self._due(_at(10)) == []

    def test_product_switched_on_alone(self, placement: Placement) -> None:
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        Placement.objects.create(
            site=placement.site,
            product=clideo,
            status=PlacementStatus.PUBLISHED,
            article_url=URL,
        )
        assert self._due(_at(10)) == [placement.pk]


class TestAlert:
    def _check(self, placement: Placement, day: int, month_day_offset: int = 0) -> bool:
        now = _at(day) + timedelta(days=month_day_offset)
        return indexation.check_placement(placement, manual=False, now=now).alert

    def test_after_thirty_days_from_publication_once(
        self, placement: Placement, serp: FakeSearch
    ) -> None:
        # Опубликовано 1 октября 12:00: 31-го в 06:00 — ещё 29 дней с хвостом.
        assert not self._check(placement, 30)
        assert not self._check(placement, 31)
        assert self._check(placement, 31, 1)  # 1 ноября: 30 дней
        assert not self._check(placement, 31, 2)
        alerted = [c for c in _checks(placement) if c.result and c.result.get("alert")]
        assert len(alerted) == 1
        assert alerted[0].result is not None
        assert alerted[0].result["failing_days"] == 30

    def test_without_date_counted_from_first_failure(
        self, make_placement: Callable[..., Placement], serp: FakeSearch
    ) -> None:
        placement = make_placement(published_at=None)
        assert not self._check(placement, 1)
        assert not self._check(placement, 30)
        assert self._check(placement, 31)

    def test_dropped_out_counted_from_first_failure_after_success(
        self, placement: Placement, serp: FakeSearch
    ) -> None:
        serp.urls = [URL]
        self._check(placement, 4)
        serp.urls = []
        assert not self._check(placement, 5)
        assert not self._check(placement, 5, 29)
        assert self._check(placement, 5, 30)

    def test_new_series_alerts_again(self, placement: Placement, serp: FakeSearch) -> None:
        assert self._check(placement, 1, 40)
        serp.urls = [URL]
        self._check(placement, 1, 41)
        serp.urls = []
        assert not self._check(placement, 1, 60)
        assert not self._check(placement, 1, 89)
        assert self._check(placement, 1, 90)

    def test_zero_days_alerts_on_first_failure(
        self, placement: Placement, product: Product, serp: FakeSearch
    ) -> None:
        set_product_setting(product.pk, "INDEXATION_SCHEDULE", {**SCHEDULE, "alert_after_days": 0})
        assert self._check(placement, 4)


class TestReportAlerts:
    def test_one_event_for_all_new_alerts(
        self,
        make_placement: Callable[..., Placement],
        serp: FakeSearch,
        sentry_events: list[dict[str, Any]],
    ) -> None:
        first, second = make_placement(), make_placement(article_url="https://x.com/b")
        for placement in (first, second):
            indexation.check_placement(placement, manual=False, now=_at(31, 6) + timedelta(days=1))
        assert indexation.report_alerts(None, _at(31, 7) + timedelta(days=1)) == 2
        (event,) = sentry_events
        message = event["logentry"]["message"]
        assert message.startswith("Статьи не в индексе дольше срока: 2")
        assert "https://x.com/b — Convertio" in message
        assert f"размещение #{first.pk}" in message
        assert event["fingerprint"] == ["indexation-alert", "2026-11-01"]

    def test_only_alerts_after_since(
        self, placement: Placement, serp: FakeSearch, sentry_events: list[dict[str, Any]]
    ) -> None:
        indexation.check_placement(placement, manual=False, now=_at(31, 6) + timedelta(days=1))
        assert indexation.report_alerts(_at(31, 7) + timedelta(days=1), timezone.now()) == 0
        assert sentry_events == []

    def test_nothing_to_report(self, serp: FakeSearch, sentry_events: list[dict[str, Any]]) -> None:
        assert indexation.report_alerts(None, timezone.now()) == 0
        assert sentry_events == []


class TestTasks:
    def test_schedule_run_checks_due_placements(
        self, make_placement: Callable[..., Placement], serp: FakeSearch
    ) -> None:
        due = make_placement(published_at=timezone.now() - timedelta(days=5))
        make_placement(published_at=timezone.now())  # рано
        serp.urls = [URL]
        assert tasks.indexation_schedule_run.delay().get() == 1
        due.refresh_from_db()
        assert due.is_indexed is True
        (check,) = Check.objects.all()
        assert check.entity_id == due.pk
        assert check.next_check_at is not None
        # run_id запуска расписания протянут до проверки.
        run = TaskRun.objects.get(task_name="indexation_schedule_run")
        assert check.run_id == run.run_id

    def test_repeated_scheduled_check_does_nothing(
        self, placement: Placement, serp: FakeSearch
    ) -> None:
        placement.published_at = timezone.now() - timedelta(days=5)
        placement.save()
        serp.urls = [URL]
        tasks.check_indexation.delay(placement.pk).get()
        tasks.check_indexation.delay(placement.pk).get()
        assert len(serp.calls) == 1
        assert len(_checks(placement)) == 1

    def test_manual_check_always_runs(self, placement: Placement, serp: FakeSearch) -> None:
        placement.skip_checks = True
        placement.save()
        serp.urls = [URL]
        tasks.check_indexation.delay(placement.pk, manual=True).get()
        tasks.check_indexation.delay(placement.pk, manual=True).get()
        assert len(serp.calls) == 2

    def test_missing_placement_or_url(
        self, make_placement: Callable[..., Placement], serp: FakeSearch
    ) -> None:
        tasks.check_indexation.delay(10**9, manual=True).get()
        tasks.check_indexation.delay(make_placement(article_url=None).pk, manual=True).get()
        assert serp.calls == []

    def test_alerts_since_previous_successful_run(
        self,
        make_placement: Callable[..., Placement],
        serp: FakeSearch,
        sentry_events: list[dict[str, Any]],
    ) -> None:
        now = timezone.now()
        TaskRun.objects.create(
            task_name="indexation_alerts",
            status=TaskStatus.SUCCESS,
            started_at=now - timedelta(hours=1),
        )
        # Неудачный прошлый запуск окно не сдвигает.
        TaskRun.objects.create(
            task_name="indexation_alerts",
            status=TaskStatus.FAILED,
            started_at=now - timedelta(minutes=30),
        )
        reported = make_placement(article_url="https://x.com/reported")
        new = make_placement(article_url="https://x.com/new")
        indexation.check_placement(reported, manual=False, now=now - timedelta(hours=2))
        indexation.check_placement(new, manual=False, now=now - timedelta(minutes=40))
        Check.objects.update(result={"url": URL, "alert": True, "failing_days": 30})
        # Пометка до прошлого удачного запуска уже сообщена — в сводке одна новая.
        assert tasks.indexation_alerts.delay().get() == 1
        (event,) = sentry_events
        assert event["extra"]["placement_ids"] == [new.pk]

    def test_throttle_is_shared_by_all_checks(self) -> None:
        assert tasks.SERP_THROTTLE.key_for((1,), {}) == tasks.SERP_THROTTLE.key_for((2,), {})
