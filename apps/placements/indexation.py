"""Проверка индексации статьи размещения (E2-03, ADR-042).

Статья в индексе, если её адрес есть в выдаче Google по запросу
`site:<адрес статьи>`, а если там нет — по запросу из самого адреса статьи
(ADR-042). `site:` в Google неточный: страницу, которая есть в индексе,
он иногда не показывает — обычный поиск по адресу её находит. Второй
запрос — только для не найденных первым. Засчитывается только сам адрес:
другие страницы площадки или того же раздела — нет.

Сроки — настройка `INDEXATION_SCHEDULE` (04-DOMAIN-RULES.md §4): первая
проверка через `first_check_days` после публикации, пока статьи нет в
индексе — каждые `retry_days`, в индексе — раз в `recheck_days`. Срок
считается в днях от полуночи (TIME_ZONE): ежедневный запуск в 06:00
застанет проверку, сделанную вчера в 06:05, — сутки ровно до секунды
сдвинули бы её на день.

Кого проверяем по расписанию: опубликованные размещения с адресом
статьи, без пометки «не проверять», у продуктов, где расписание
включено. Нет даты публикации — проверяем сразу. Сменили адрес статьи —
проверяем заново, при ближайшем запуске.

Каждая проверка — строка `checks` (`placement`, `indexation`) и
`placements.is_indexed`. Статья не в индексе `alert_after_days` дней
подряд — в строке пометка `alert`, одна на серию неудач. Раз в сутки
`report_alerts` собирает новые пометки в одно оповещение (событие
GlitchTip, ADR-042); в Telegram их отправит E9-04.

Проверка адреса из поля в «Размещениях» (E9-12, `find_url`) — тот же поиск, но
без записи: ни в журнал, ни в размещение. Тратится только запрос к выдаче
(`api_usage`).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import sentry_sdk
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.db.models.fields.json import KT
from django.utils import timezone

from apps.content.domain_settings import IndexationSchedule, indexation_schedule
from apps.integrations.serp import SerpPage, search
from apps.observability.models import Check, CheckStatus, Performer
from apps.placements.models import Placement, PlacementStatus

logger = logging.getLogger(__name__)

ENTITY_TYPE = "placement"
CHECK_TYPE = "indexation"
# Глубина выдачи: по `site:` с полным адресом статья, если она в индексе,
# стоит в первых строках; 10 результатов — один кредит Serper.
DEPTH = 10


@dataclass(frozen=True)
class Outcome:
    """Итог проверки: что записано в журнал."""

    check: Check
    indexed: bool
    alert: bool


@dataclass(frozen=True)
class Found:
    """Что дал поиск статьи в выдаче."""

    position: int | None
    method: str | None
    queries: list[str]
    seen: list[str]

    @property
    def indexed(self) -> bool:
        return self.position is not None


def normalize_url(url: str) -> str:
    """Адрес для сравнения: без схемы, `www.`, порта, `#…` и `/` в конце.

    Домен — без учёта регистра, путь — с учётом (на сервере это разные
    страницы); `%`-кодирование раскрыто. Из запроса `?…` убраны метки
    отслеживания (`TRACKING_PARAMS`), остальное — по алфавиту.
    """
    raw = url.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    host = (parts.hostname or "").removeprefix("www.")
    path = unquote(parts.path).rstrip("/")
    params = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    )
    query = f"?{urlencode(params)}" if params else ""
    return f"{host}{path}{query}"


# Метки, которые Google и рекламные системы дописывают к адресу; страница та же.
# `srsltid` Google ставит в выдаче страницам магазинов (Shopify).
TRACKING_PARAMS = frozenset({"srsltid", "gclid", "gbraid", "wbraid", "fbclid", "yclid", "msclkid"})


def _is_tracking(name: str) -> bool:
    name = name.lower()
    return name in TRACKING_PARAMS or name.startswith("utm_")


def _without_query(normalized: str) -> str:
    return normalized.split("?", 1)[0]


def search_query(url: str) -> str:
    """Запрос к выдаче: `site:` и адрес без схемы и `#…`."""
    raw = url.strip().split("#", 1)[0]
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return f"site:{raw}"


def same_page(article_url: str, found_url: str) -> bool:
    """Адрес из выдачи — та же статья.

    У адреса статьи нет `?…` — параметры адреса из выдачи не важны: Google
    дописывает свои метки, и не все из них известны заранее. Есть — должны
    совпасть, кроме меток отслеживания.
    """
    target, found = normalize_url(article_url), normalize_url(found_url)
    if "?" not in target:
        found = _without_query(found)
    return found == target


def url_query(url: str) -> str:
    """Запасной запрос — сам адрес статьи, без оператора: как его ищет человек."""
    return url.strip().split("#", 1)[0]


def find_position(url: str, page: SerpPage) -> int | None:
    """Место статьи в выдаче или None, если её там нет."""
    for result in page.results:
        if same_page(url, result.url):
            return result.position
    return None


def day_start(day: date) -> datetime:
    """Полночь дня в TIME_ZONE."""
    return datetime.combine(day, time.min, tzinfo=timezone.get_current_timezone())


def next_check_at(indexed: bool, schedule: IndexationSchedule, now: datetime) -> datetime:
    days = schedule.recheck_days if indexed else schedule.retry_days
    return day_start(timezone.localdate(now) + timedelta(days=days))


def checkable() -> Q:
    """Размещения, которые вообще можно проверить: опубликованы, адрес статьи есть."""
    return Q(status=PlacementStatus.PLACED, article_url__isnull=False) & ~Q(article_url="")


def _checks_of(placement_id: Any) -> QuerySet[Check]:
    return Check.objects.filter(
        entity_type=ENTITY_TYPE, entity_id=placement_id, check_type=CHECK_TYPE
    )


def _due(product_id: int, schedule: IndexationSchedule, now: datetime) -> QuerySet[Placement]:
    """Размещения продукта, которым пора на проверку по расписанию.

    `Subquery` — вложенный SELECT: для каждого размещения берёт поле его
    последней проверки индексации, `OuterRef("pk")` — id размещения из
    внешнего запроса. Весь отбор — один запрос к базе.
    """
    latest = _checks_of(OuterRef("pk")).order_by("-checked_at", "-id")
    # Первая проверка — в полночь через `first_check_days` после дня
    # публикации: пора всем, опубликованным раньше этой границы.
    first_cutoff = day_start(
        timezone.localdate(now) - timedelta(days=schedule.first_check_days - 1)
    )
    never_checked = Q(last_check__isnull=True) & (
        Q(published_at__isnull=True) | Q(published_at__lt=first_cutoff)
    )
    checked_before = Q(last_check__isnull=False) & (
        Q(last_due__isnull=True)
        | Q(last_due__lte=now)
        # Адрес статьи сменили после проверки. Проверка человеком без
        # адреса в журнале срок не сбивает.
        | (Q(last_url__isnull=False) & ~Q(last_url=F("article_url")))
    )
    return (
        Placement.objects.filter(checkable(), product_id=product_id, skip_checks=False)
        .annotate(
            last_check=Subquery(latest.values("id")[:1]),
            last_due=Subquery(latest.values("next_check_at")[:1]),
            last_url=Subquery(latest.annotate(url=KT("result__url")).values("url")[:1]),
        )
        .filter(never_checked | checked_before)
    )


def due_placement_ids(now: datetime) -> list[int]:
    """Кого проверить сейчас по расписанию — у всех продуктов с включённым расписанием."""
    product_ids = (
        Placement.objects.filter(checkable(), skip_checks=False)
        .values_list("product_id", flat=True)
        .distinct()
        .order_by("product_id")
    )
    ids: list[int] = []
    for product_id in product_ids:
        schedule = indexation_schedule(product_id)
        if schedule.enabled:
            ids += list(_due(product_id, schedule, now).order_by("pk").values_list("pk", flat=True))
    return ids


def is_due(placement: Placement, now: datetime) -> bool:
    """Пора ли размещению на плановую проверку — тот же отбор, что у расписания."""
    schedule = indexation_schedule(placement.product_id)
    if not schedule.enabled:
        return False
    return _due(placement.product_id, schedule, now).filter(pk=placement.pk).exists()


def check_placement(placement: Placement, *, manual: bool, now: datetime | None = None) -> Outcome:
    """Ищет статью в выдаче и записывает результат.

    Поиск — до транзакции: строка расхода `api_usage` пишется сразу и не
    откатится вместе с записью проверки, если та упадёт. Выдача — всегда
    свежая (`fresh`), не из кеша.
    """
    url = placement.article_url
    if not url:
        raise ValueError(f"У размещения {placement.pk} нет адреса статьи")
    found = search_article(url)
    result: dict[str, Any] = {"url": url, "manual": manual, "queries": found.queries}
    if found.method is not None:
        result["found_by"] = found.method
    seen = found.seen
    position = found.position
    now = now or timezone.now()
    indexed = found.indexed
    result["position"] = position
    schedule = indexation_schedule(placement.product_id)

    with transaction.atomic():
        if not indexed and seen:
            # Что было в выдаче вместо статьи — видно в истории, почему не засчитано.
            result["seen"] = seen
        alert = False
        if not indexed:
            since = failing_since(placement, now)
            result["failing_days"] = (now - since).days
            alert = _alert_due(placement, since, schedule, now)
        if alert:
            result["alert"] = True
        check = Check.objects.create(
            entity_type=ENTITY_TYPE,
            entity_id=placement.pk,
            check_type=CHECK_TYPE,
            status=CheckStatus.OK if indexed else CheckStatus.FAILED,
            result=result,
            performed_by=Performer.SYSTEM,
            checked_at=now,
            next_check_at=next_check_at(indexed, schedule, now),
        )
        placement.is_indexed = indexed
        placement.indexed_checked_at = now
        placement.save(update_fields=["is_indexed", "indexed_checked_at", "updated_at"])

    logger.info(
        "индексация проверена",
        extra={
            "placement_id": placement.pk,
            "url": url,
            "indexed": indexed,
            "position": position,
            "manual": manual,
            "alert": alert,
        },
    )
    return Outcome(check=check, indexed=indexed, alert=alert)


def search_article(url: str) -> Found:
    """Ищет адрес в выдаче: `site:адрес`, не нашлось — сам адрес (ADR-042).

    Выдача — всегда свежая (`fresh`), не из кеша; каждый запрос — строка
    расхода `api_usage` (пишет `search`).
    """
    queries: list[str] = []
    seen: list[str] = []
    for method, query in (("site", search_query(url)), ("url", url_query(url))):
        page = search(query, depth=DEPTH, fresh=True)
        queries.append(query)
        position = find_position(url, page)
        if position is not None:
            return Found(position=position, method=method, queries=queries, seen=seen)
        seen += [found.url for found in page.results if found.url not in seen]
    return Found(position=None, method=None, queries=queries, seen=seen)


def page_url(raw: str) -> str:
    """Адрес из поля «Адрес страницы»: без пробелов по краям, без схемы — `https://`.

    Не адрес — `ValidationError` с текстом для окошка.
    """
    url = raw.strip()
    if not url:
        raise ValidationError("Вставьте адрес страницы.")
    if "://" not in url:
        url = f"https://{url}"
    URLValidator(schemes=["http", "https"], message=f"Это не адрес страницы: {raw.strip()}")(url)
    return url


def find_url(url: str) -> Found:
    """Проверка адреса из поля «Адрес страницы» (E9-12): только поиск, без записи."""
    found = search_article(url)
    logger.info(
        "адрес проверен на индексацию",
        extra={"url": url, "indexed": found.indexed, "position": found.position},
    )
    return found


def failing_since(placement: Placement, now: datetime) -> datetime:
    """С какого момента статья подряд не в индексе.

    Ни разу не была в индексе — с публикации (нет даты — с первой
    неудачной проверки). Была и выпала — с первой неудачи после последнего
    успеха. Неудач ещё нет — сейчас.
    """
    checks = _checks_of(placement.pk)
    last_ok = (
        checks.filter(status=CheckStatus.OK)
        .order_by("-checked_at")
        .values_list("checked_at", flat=True)
        .first()
    )
    if last_ok is None and placement.published_at is not None:
        return placement.published_at
    failures = checks.filter(status=CheckStatus.FAILED)
    if last_ok is not None:
        failures = failures.filter(checked_at__gt=last_ok)
    first_failure = failures.order_by("checked_at").values_list("checked_at", flat=True).first()
    return first_failure or now


def _alert_due(
    placement: Placement, since: datetime, schedule: IndexationSchedule, now: datetime
) -> bool:
    """Пора оповестить: серия неудач с `since` дольше срока, и в ней ещё не оповещали."""
    if now - since < timedelta(days=schedule.alert_after_days):
        return False
    already = _checks_of(placement.pk).filter(
        status=CheckStatus.FAILED, checked_at__gte=since, result__alert=True
    )
    return not already.exists()


def report_alerts(since: datetime | None, until: datetime) -> int:
    """Одно оповещение обо всех новых пометках `alert` за (since, until]. Возвращает их число.

    Событие в GlitchTip — запись ERROR. Отпечаток (`fingerprint`) с датой:
    у GlitchTip каждое утро — новая «проблема» и новое письмо, а не ещё одно
    событие в старой, о которой письмо уже было.
    """
    checks = _checks_of_type().filter(result__alert=True, checked_at__lte=until)
    if since is not None:
        checks = checks.filter(checked_at__gt=since)
    alerts = list(checks.order_by("checked_at", "id"))
    if not alerts:
        logger.info("новых статей без индекса дольше срока нет")
        return 0
    placements = Placement.objects.select_related("product").in_bulk(
        [check.entity_id for check in alerts]
    )
    lines = []
    for check in alerts:
        placement = placements.get(check.entity_id)
        product = placement.product.name if placement else "?"
        result = check.result or {}
        days = result.get("failing_days", "?")
        lines.append(
            f"- {result.get('url', '?')} — {product}, размещение #{check.entity_id},"
            f" не в индексе {days} дн."
        )
    day = timezone.localdate(until)
    message = f"Статьи не в индексе дольше срока: {len(alerts)}\n" + "\n".join(lines)
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["indexation-alert", day.isoformat()]
        logger.error(
            message, extra={"placement_ids": [check.entity_id for check in alerts], "day": str(day)}
        )
    return len(alerts)


def _checks_of_type() -> QuerySet[Check]:
    return Check.objects.filter(entity_type=ENTITY_TYPE, check_type=CHECK_TYPE)
