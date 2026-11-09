"""Задачи очереди блока размещений: проверка индексации (E2-03, ADR-042).

- `indexation_schedule_run` — beat раз в сутки: ставит в очередь проверку
  каждому размещению, которому пора (`indexation.due_placement_ids`).
  Расписание выключено в настройке — никого.
- `check_indexation` — одна статья. Плановая проверка, которой уже не пора
  (задачу отдали повторно после остановки воркера, статью проверили
  кнопкой), ничего не делает: второй строки в журнале и второго платного
  запроса не будет. Ручная (`manual`, кнопка в карточке) проверяет всегда.
- `indexation_alerts` — beat раз в сутки, после проверок: одно оповещение
  о статьях, которые не в индексе дольше срока.
- `check_url_indexation` — адрес из поля «Адрес страницы» в «Размещениях»
  (E9-12): итог — в кеш на `URL_RESULT_TTL` под ключом, который выдала
  страница, только чтобы показать окошко. Никуда не пишется; повтор задачи
  перезапишет тот же ключ.
- `collaborator_refresh` — кнопка «Обновить из Collaborator» (E11-01, ADR-079):
  все сделки из API в снимок, ход и итог — в кеш под ключом страницы. По
  расписанию не ставится — только кнопкой. Одна попытка: человек ждёт итог в
  окне, повтор через минуту ему не поможет — он нажмёт ещё раз.

Как логика проверки устроена — `apps/placements/indexation.py`.
"""

import logging
from typing import Any

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from apps.integrations.collaborator import CollaboratorClient, CollaboratorError
from apps.observability.models import TaskRun, TaskStatus
from apps.placements import indexation
from apps.placements.collaborator import deals, service
from apps.placements.models import Placement
from apps.sites.models import Product
from config.queue import QueueTask
from config.throttle import Throttle

logger = logging.getLogger(__name__)

# Запросы к выдаче — не чаще пяти в секунду на всех: первый прогон ставит
# сотни проверок разом.
SERP_THROTTLE = Throttle("serp", per_second=5, key=lambda *args, **kwargs: "all")


@shared_task(base=QueueTask, name="indexation_schedule_run")
def indexation_schedule_run() -> int:
    ids = indexation.due_placement_ids(timezone.now())
    for placement_id in ids:
        check_indexation.delay(placement_id)
    logger.info("проверки индексации поставлены в очередь", extra={"count": len(ids)})
    return len(ids)


@shared_task(base=QueueTask, name="check_indexation", throttle=SERP_THROTTLE)
def check_indexation(placement_id: int, *, manual: bool = False) -> None:
    placement = Placement.objects.filter(pk=placement_id).first()
    if placement is None:
        logger.warning("размещения нет", extra={"placement_id": placement_id})
        return
    if not placement.article_url:
        logger.info("у размещения нет адреса статьи", extra={"placement_id": placement_id})
        return
    if not manual and not indexation.is_due(placement, timezone.now()):
        logger.info("проверка индексации уже не нужна", extra={"placement_id": placement_id})
        return
    indexation.check_placement(placement, manual=manual)


# Сколько ждёт итог проверки адреса: страница спрашивает его, пока ждёт задачу.
URL_RESULT_TTL = 15 * 60


def url_result_key(key: str) -> str:
    return f"url-indexation:{key}"


@shared_task(base=QueueTask, name="check_url_indexation", throttle=SERP_THROTTLE)
def check_url_indexation(url: str, key: str) -> None:
    found = indexation.find_url(url)
    result: dict[str, Any] = {"url": url, "indexed": found.indexed, "position": found.position}
    cache.set(url_result_key(key), result, URL_RESULT_TTL)


@shared_task(base=QueueTask, name="indexation_alerts")
def indexation_alerts() -> int:
    # Сообщаем о пометках с прошлого удачного запуска этой же задачи:
    # пропущенный день (воркер лежал) не теряется, а попадёт в следующую сводку.
    previous = (
        TaskRun.objects.filter(task_name="indexation_alerts", status=TaskStatus.SUCCESS)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    return indexation.report_alerts(previous, timezone.now())


# Ход и итог «Обновить из Collaborator» ждут страницу столько; пока задача идёт,
# второе нажатие получает её же ключ (`REFRESH_RUNNING`).
REFRESH_TTL = 15 * 60
REFRESH_RUNNING = "collaborator-refresh:running"


def refresh_key(key: str) -> str:
    return f"collaborator-refresh:{key}"


@shared_task(base=QueueTask, name="collaborator_refresh", max_retries=0)
def collaborator_refresh(key: str, product_id: int | None = None) -> dict[str, Any]:
    def progress(page: int, pages: int) -> None:
        state = {"state": "running", "page": page, "pages": pages}
        cache.set(refresh_key(key), state, REFRESH_TTL)

    cache.set(refresh_key(key), {"state": "running", "page": 0, "pages": 0}, REFRESH_TTL)
    try:
        refreshed = deals.refresh(CollaboratorClient.from_env(), on_page=progress)
    except CollaboratorError as error:
        cache.set(refresh_key(key), {"state": "failed", "error": str(error)}, REFRESH_TTL)
        raise
    finally:
        if cache.get(REFRESH_RUNNING) == key:
            cache.delete(REFRESH_RUNNING)
    result: dict[str, Any] = {"state": "done", "deals": refreshed.deals}
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    if product is not None:
        found = service.report(product)
        result.update(
            text=found.summary,
            ready=len(found.groups.ready),
            fill=len(found.groups.fill),
            differ=len(found.groups.differ),
            missing=len(found.groups.missing),
        )
    else:
        result["text"] = f"Забрано сделок: {refreshed.deals}."
    cache.set(refresh_key(key), result, REFRESH_TTL)
    logger.info(
        "сделки Collaborator забраны",
        extra={"deals": refreshed.deals, "added": refreshed.added, "removed": refreshed.removed},
    )
    return {"deals": refreshed.deals, "added": refreshed.added, "removed": refreshed.removed}
