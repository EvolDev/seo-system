"""Клиент SERP API: разбор ответа Serper, кеш, расход, дневной лимит (E2-02).

В сеть тесты не ходят: у Serper подменён транспорт httpx, у поиска —
провайдер.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from celery import shared_task
from celery.exceptions import Ignore
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.utils import timezone
from pytest_django import Settings

from apps.integrations.serp import SerpBudgetExceeded, SerpError, SerpPage, SerpResult, search
from apps.integrations.serp import client as serp_client
from apps.integrations.serp.serper import URL, SerperProvider
from apps.integrations.serp.types import ProviderAnswer, next_midnight
from apps.observability.models import ApiUsage, TaskRun, TaskStatus
from config.queue import RUN_ID_ARG, QueueTask
from config.run_id import bind_run_id

pytestmark = pytest.mark.django_db

KEY = "test-serper-key-0123456789abcdef"

# Ответ Serper той же формы, что у настоящего API.
SERPER_BODY: dict[str, Any] = {
    "searchParameters": {"q": "site:example.com", "gl": "us", "num": 10, "type": "search"},
    "organic": [
        {
            "title": "Example — главная",
            "link": "https://Example.com/",
            "snippet": "Первый результат",
            "position": 1,
        },
        {
            "title": "Статья",
            "link": "https://example.com/blog/article",
            "snippet": "Второй результат",
            "position": 2,
            "date": "Sep 1, 2026",
        },
    ],
    "credits": 1,
}


def _serper(
    handler: Callable[[httpx.Request], httpx.Response], cents_per_1000: int = 100
) -> SerperProvider:
    return SerperProvider(KEY, cents_per_1000, transport=httpx.MockTransport(handler))


def _answer(body: dict[str, Any], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


class TestSerper:
    def test_parses_results_and_cost(self) -> None:
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=SERPER_BODY)

        answer = _serper(handler).fetch("site:example.com", 10, "us")

        (request,) = sent
        assert str(request.url) == URL
        assert request.headers["X-API-KEY"] == KEY
        assert json.loads(request.content) == {"q": "site:example.com", "gl": "us", "num": 10}
        page = answer.page
        assert page.results == (
            SerpResult(
                1,
                "https://Example.com/",
                "example.com",
                "Example — главная",
                "Первый результат",
            ),
            SerpResult(
                2,
                "https://example.com/blog/article",
                "example.com",
                "Статья",
                "Второй результат",
            ),
        )
        assert page.total_estimate is None
        assert (answer.endpoint, answer.units) == ("search", 1)
        # Кредит по $1 за тысячу — десятая доля цента.
        assert answer.cost_cents == Decimal("0.1")

    def test_cost_follows_pack_price_and_credits(self) -> None:
        body = {**SERPER_BODY, "credits": 2}
        answer = _serper(_answer(body), cents_per_1000=75).fetch("q", 100, "us")
        assert answer.units == 2
        assert answer.cost_cents == Decimal("0.15")

    def test_total_estimate_when_present(self) -> None:
        body = {**SERPER_BODY, "searchInformation": {"totalResults": "4230"}}
        assert _serper(_answer(body)).fetch("q", 10, "us").page.total_estimate == 4230

    def test_no_results(self) -> None:
        body = {"searchParameters": {}, "credits": 1}
        assert _serper(_answer(body)).fetch("q", 10, "us").page.results == ()

    @pytest.mark.parametrize(
        ("status", "body", "text"),
        [
            (403, {"message": "Unauthorized.", "statusCode": 403}, "403: Unauthorized."),
            (400, {"message": "Not enough credits", "statusCode": 400}, "Not enough credits"),
            (500, {}, "500"),
        ],
    )
    def test_error_answer(self, status: int, body: dict[str, Any], text: str) -> None:
        with pytest.raises(SerpError, match=text):
            _serper(_answer(body, status)).fetch("q", 10, "us")

    def test_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("нет ответа", request=request)

        with pytest.raises(SerpError, match="недоступен"):
            _serper(handler).fetch("q", 10, "us")

    def test_without_credits_no_silent_spending(self) -> None:
        body = {k: v for k, v in SERPER_BODY.items() if k != "credits"}
        with pytest.raises(SerpError, match="кредитов"):
            _serper(_answer(body)).fetch("q", 10, "us")

    def test_key_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        with pytest.raises(ImproperlyConfigured, match="SERPER_API_KEY"):
            SerperProvider.from_settings()


class FakeProvider:
    """Провайдер без сети: считает вызовы, каждый стоит `cost` центов."""

    name = "serper"

    def __init__(self, cost: str = "0.1") -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.cost = Decimal(cost)

    def fetch(self, query: str, depth: int, country: str) -> ProviderAnswer:
        self.calls.append((query, depth, country))
        page = SerpPage(
            query=query,
            depth=depth,
            country=country,
            results=(SerpResult(1, "https://example.com/a", "example.com", "A", ""),),
            total_estimate=None,
            fetched_at=timezone.now(),
        )
        return ProviderAnswer(page=page, endpoint="search", units=1, cost_cents=self.cost)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setitem(serp_client.PROVIDERS, "serper", lambda: fake)
    return fake


@pytest.fixture
def budget(settings: Settings) -> int:
    settings.SERP_PROVIDER = "serper"
    settings.SERP_DAILY_BUDGET_CENTS = 500
    settings.SERP_CACHE_HOURS = 24
    return 500


def _spend(cents: str, provider: str = "serper", when: datetime | None = None) -> None:
    ApiUsage.objects.create(
        provider=provider, cost_cents=Decimal(cents), created_at=when or timezone.now()
    )


class TestSearch:
    def test_returns_page_and_records_cost_at_once(
        self, provider: FakeProvider, budget: int
    ) -> None:
        run_id = uuid4()
        with bind_run_id(run_id):
            page = search("site:example.com")
        assert [r.url for r in page.results] == ["https://example.com/a"]
        (usage,) = ApiUsage.objects.all()
        assert (usage.provider, usage.endpoint, usage.units) == ("serper", "search", 1)
        assert usage.cost_cents == Decimal("0.1")
        assert usage.currency == "USD"
        assert usage.run_id == run_id

    def test_same_query_same_day_costs_nothing(self, provider: FakeProvider, budget: int) -> None:
        first = search("site:example.com", depth=10, country="us")
        # Лишние пробелы и регистр страны — тот же запрос.
        second = search("  site:example.com ", depth=10, country="US")
        assert second == first
        assert len(provider.calls) == 1
        assert ApiUsage.objects.count() == 1

    def test_other_depth_or_country_is_other_query(
        self, provider: FakeProvider, budget: int
    ) -> None:
        search("site:example.com")
        search("site:example.com", depth=20)
        search("site:example.com", country="gb")
        assert len(provider.calls) == 3
        assert ApiUsage.objects.count() == 3

    def test_cache_expires(self, provider: FakeProvider, budget: int, settings: Settings) -> None:
        # 0 часов — кеш сразу устарел: второй запрос снова к провайдеру.
        settings.SERP_CACHE_HOURS = 0
        search("site:example.com")
        search("site:example.com")
        assert len(provider.calls) == 2

    @pytest.mark.parametrize(
        ("query", "depth", "country"),
        [("  ", 10, "us"), ("q", 0, "us"), ("q", 101, "us"), ("q", 10, "usa"), ("q", 10, "рф")],
    )
    def test_bad_arguments(
        self, provider: FakeProvider, budget: int, query: str, depth: int, country: str
    ) -> None:
        with pytest.raises(ValueError):
            search(query, depth=depth, country=country)
        assert provider.calls == []

    def test_unknown_provider(self, settings: Settings) -> None:
        settings.SERP_PROVIDER = "dataforseo"
        with pytest.raises(ImproperlyConfigured, match="serper"):
            search("q")


class TestBudget:
    def test_over_budget_no_request(self, provider: FakeProvider, budget: int) -> None:
        _spend("300")
        _spend("200")
        with pytest.raises(SerpBudgetExceeded) as caught:
            search("site:example.com")
        assert provider.calls == []
        assert caught.value.until == next_midnight(timezone.localtime())
        assert "500" in caught.value.reason

    def test_under_budget_goes_on(self, provider: FakeProvider, budget: int) -> None:
        _spend("499.9999")
        search("site:example.com")
        assert len(provider.calls) == 1

    def test_yesterday_and_other_apis_do_not_count(
        self, provider: FakeProvider, budget: int
    ) -> None:
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        _spend("1000", when=start - timedelta(seconds=1))
        _spend("1000", provider="ahrefs")
        search("site:example.com")
        assert len(provider.calls) == 1

    def test_cached_answer_ignores_budget(self, provider: FakeProvider, budget: int) -> None:
        search("site:example.com")
        _spend("500")
        search("site:example.com")
        assert len(provider.calls) == 1

    def test_one_alert_a_day(
        self,
        provider: FakeProvider,
        budget: int,
        sentry_events: list[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _spend("500")
        with caplog.at_level(logging.INFO, logger=serp_client.__name__):
            for _ in range(3):
                with pytest.raises(SerpBudgetExceeded):
                    search("site:example.com")
        levels = [r.levelname for r in caplog.records if "лимит" in r.getMessage()]
        assert levels == ["ERROR", "INFO", "INFO"]
        assert len(sentry_events) == 1

    def test_task_stops_until_midnight(
        self, provider: FakeProvider, budget: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Задача очереди с исчерпанным лимитом встаёт на паузу, а не падает.
        _spend("500")
        sent: list[dict[str, Any]] = []

        def fake_apply_async(self: QueueTask, *args: Any, **options: Any) -> None:
            sent.append(options)

        monkeypatch.setattr(QueueTask, "apply_async", fake_apply_async)
        task_id, run_id = str(uuid4()), str(uuid4())
        find.push_request(
            id=task_id,
            args=["site:example.com"],
            kwargs={RUN_ID_ARG: run_id},
            retries=0,
            called_directly=False,
            is_eager=False,
            delivery_info={},
        )
        try:
            with pytest.raises(Ignore):
                find("site:example.com", run_id=run_id)
        finally:
            find.pop_request()

        assert provider.calls == []
        assert len(sent) == 1
        (run,) = TaskRun.objects.filter(task_name="test_serp.find")
        assert run.status == TaskStatus.RUNNING
        assert run.run_id == UUID(run_id)
        assert "дневной лимит" in (run.payload or {})["waiting"]["reason"]


@shared_task(base=QueueTask, name="test_serp.find")
def find(query: str) -> int:
    return len(search(query).results)


class TestTypes:
    def test_page_survives_cache_round_trip(self) -> None:
        page = SerpPage(
            query="q",
            depth=10,
            country="us",
            results=(SerpResult(1, "https://a.com/", "a.com", "A", "s"),),
            total_estimate=12,
            fetched_at=datetime(2026, 9, 30, 12, tzinfo=UTC),
        )
        assert SerpPage.from_dict(page.to_dict()) == page

    def test_next_midnight_in_local_zone(self) -> None:
        moscow = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 9, 30, 23, 59, tzinfo=moscow)
        assert next_midnight(now) == datetime(2026, 10, 1, tzinfo=moscow)

    def test_budget_error_survives_pickle(self) -> None:
        import pickle

        error = SerpBudgetExceeded(
            datetime(2026, 10, 1, tzinfo=UTC), spent=Decimal("500.1"), budget=500
        )
        copy = pickle.loads(pickle.dumps(error))
        assert (copy.until, copy.spent, copy.budget, copy.reason) == (
            error.until,
            error.spent,
            error.budget,
            error.reason,
        )


class TestCommand:
    def test_prints_results_and_cost(self, provider: FakeProvider, budget: int) -> None:
        out = StringIO()
        call_command("serp_search", "site:example.com", stdout=out)
        text = out.getvalue()
        assert "1. https://example.com/a" in text
        assert "Стоимость: 0.1000 ¢" in text
        assert "Потрачено сегодня: 0.1000 ¢ из 500 ¢" in text

    def test_repeat_is_free(self, provider: FakeProvider, budget: int) -> None:
        call_command("serp_search", "site:example.com", stdout=StringIO())
        out = StringIO()
        call_command("serp_search", "site:example.com", stdout=out)
        assert "Из кеша, бесплатно." in out.getvalue()
        assert len(provider.calls) == 1

    def test_budget_is_an_error(self, provider: FakeProvider, budget: int) -> None:
        _spend("500")
        with pytest.raises(CommandError, match="дневной лимит"):
            call_command("serp_search", "site:example.com", stdout=StringIO())
