"""Поиск по выдаче Google: кеш, дневной лимит, учёт расхода (E2-02, ADR-040).

    page = search("site:example.com/article", country="us")

Провайдер выбирает настройка `SERP_PROVIDER`; вызывающий код о нём не знает.

Порядок одного поиска:
1. Тот же запрос (текст, глубина, страна) за последние `SERP_CACHE_HOURS`
   часов — ответ из кеша, денег не тратит. `fresh=True` — кеш не читается,
   ответ всё равно в него пишется: так ищет проверка, которой нужен ответ
   на сейчас, — индексация (E2-03). Вчерашний ответ, ещё живой в кеше,
   сдвинул бы ежедневную проверку на сутки.
2. Потрачено за сегодня не меньше `SERP_DAILY_BUDGET_CENTS` —
   `SerpBudgetExceeded`: задача очереди встаёт на паузу до полуночи
   (config/queue.py), разработчику — одно оповещение за сутки.
3. Запрос к провайдеру; расход — строкой в `api_usage` сразу, с run_id
   цепочки.

Лимит проверяется до запроса, поэтому последний запрос дня может выйти за
него на свою цену — на долю цента на каждый параллельный воркер.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Sum
from django.utils import timezone

from apps.observability.models import ApiUsage

from .serper import SerperProvider
from .types import SerpBudgetExceeded, SerpPage, SerpProvider, next_midnight

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, Callable[[], SerpProvider]] = {
    SerperProvider.name: SerperProvider.from_settings,
}
MAX_DEPTH = 100
CACHE_PREFIX = "serp:page"
ALERT_PREFIX = "serp:budget-alert"


def search(query: str, *, depth: int = 10, country: str = "us", fresh: bool = False) -> SerpPage:
    """Выдача Google по запросу: `depth` результатов, страна — код из двух букв.

    `fresh` — не брать ответ из кеша (см. описание модуля).
    """
    query = " ".join(query.split())
    country = country.lower()
    _validate(query, depth, country)

    key = _cache_key(query, depth, country)
    cached = None if fresh else cache.get(key)
    if cached is not None:
        logger.info("выдача из кеша", extra={"query": query, "depth": depth, "country": country})
        return SerpPage.from_dict(cached)

    provider = get_provider()
    _check_budget()
    answer = provider.fetch(query, depth, country)
    ApiUsage.objects.create(
        provider=provider.name,
        endpoint=answer.endpoint,
        units=answer.units,
        cost_cents=answer.cost_cents,
    )
    cache.set(key, answer.page.to_dict(), timeout=settings.SERP_CACHE_HOURS * 3600)
    logger.info(
        "выдача получена",
        extra={
            "query": query,
            "provider": provider.name,
            "results": len(answer.page.results),
            "cost_cents": str(answer.cost_cents),
        },
    )
    return answer.page


def get_provider() -> SerpProvider:
    name = settings.SERP_PROVIDER
    factory = PROVIDERS.get(name)
    if factory is None:
        allowed = " | ".join(sorted(PROVIDERS))
        raise ImproperlyConfigured(f"SERP_PROVIDER: ожидается {allowed}, получено {name!r}")
    return factory()


def spent_today(now: datetime | None = None) -> Decimal:
    """Сколько центов ушло на выдачу с начала суток (TIME_ZONE), у всех провайдеров."""
    local = timezone.localtime(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    total = ApiUsage.objects.filter(provider__in=PROVIDERS, created_at__gte=start).aggregate(
        total=Sum("cost_cents")
    )["total"]
    return total or Decimal(0)


def _check_budget() -> None:
    budget: int = settings.SERP_DAILY_BUDGET_CENTS
    spent = spent_today()
    if spent < budget:
        return
    now = timezone.localtime()
    error = SerpBudgetExceeded(next_midnight(now), spent=spent, budget=budget)
    extra = {"spent_cents": str(spent), "budget_cents": budget}
    # cache.add пишет, только если ключа нет, — одной командой, поэтому
    # оповещение (ERROR → событие в Sentry) уходит один раз за сутки.
    if cache.add(f"{ALERT_PREFIX}:{now.date().isoformat()}", 1, timeout=24 * 3600):
        logger.error("дневной лимит на выдачу исчерпан, поиск встал до полуночи", extra=extra)
    else:
        logger.info("дневной лимит на выдачу исчерпан", extra=extra)
    raise error


def _validate(query: str, depth: int, country: str) -> None:
    if not query:
        raise ValueError("пустой запрос")
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"глубина от 1 до {MAX_DEPTH}, получено {depth}")
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        raise ValueError(f"страна — код из двух латинских букв, получено {country!r}")


def _cache_key(query: str, depth: int, country: str) -> str:
    raw = json.dumps([query, depth, country], ensure_ascii=False)
    return f"{CACHE_PREFIX}:{hashlib.sha256(raw.encode()).hexdigest()}"
