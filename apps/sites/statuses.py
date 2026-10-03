"""Как статус площадки у продукта меняется сам (E1-12, ADR-047).

Путь площадки в работу — лесенка «Новая → Просмотрено → Одобрена → Заявка
отправлена → Размещались». Система двигает статус по ней только вперёд:
новая заявка «Размещались» не откатывает.

Заявка и публикация размещения — факты. Новый факт — размещение только что
создано или его статус только что сменился — сильнее прежнего решения
«Отбрасываю», «Отказала площадка» и «На аудите». Старый факт, повторённый
ещё раз (та же заявка в таблице при следующем импорте), решение человека не
перекрывает: отказ мог быть поставлен уже после заявки. «Чёрный список»
система не трогает никогда. Решение без факта — «Одобрена» по
запланированному размещению, отказ из таблицы или аудита — встаёт только
туда, где решения ещё нет: на «Новая» и «Просмотрено».

Правила — только для системы: размещения (`Placement.save()`), импорта
таблицы, аудита (E4). Человек в окне статуса ставит любой статус.
"""

from django.db import transaction
from django.utils import timezone

from apps.sites.models import ProductSite, SiteStatus, StatusSource
from config.changes import stamped

LADDER = (
    SiteStatus.NEW,
    SiteStatus.VIEWED,
    SiteStatus.APPROVED,
    SiteStatus.ORDERED,
    SiteStatus.PLACED,
)
# Решения ещё нет.
UNDECIDED = (SiteStatus.NEW, SiteStatus.VIEWED)
# Что ставит факт размещения: заявка в работе и публикация.
FACTS = (SiteStatus.ORDERED, SiteStatus.PLACED)
# Прежние решения, которые новый факт перекрывает. Чёрного списка здесь нет.
OVERRIDDEN_BY_FACT = (SiteStatus.DISCARDED, SiteStatus.DECLINED, SiteStatus.AUDITING)


def replaceable(target: SiteStatus, *, fact: bool = False) -> tuple[SiteStatus, ...]:
    """Статусы, которые система сама сменит на `target`.

    `fact` — `target` ставит новый факт: размещение только что создано или
    его статус только что сменился.
    """
    if target not in LADDER:
        return UNDECIDED
    behind = LADDER[: LADDER.index(target)]
    return behind + OVERRIDDEN_BY_FACT if fact and target in FACTS else behind


def advance(
    site_id: int, product_id: int, target: SiteStatus, *, placement_id: int | None = None
) -> bool:
    """Новый факт размещения: поставить площадке у продукта `target`, если можно.

    True — статус сменился. Один UPDATE с условием на текущий статус: строка
    проверяется и меняется разом, решение, которое человек сохранил в ту же
    секунду, не перезаписывается. `update()` не вызывает `save()`, поэтому
    время правки и снятие пометки «импортирована без решения» — здесь же.

    В истории (ADR-049) смена — «по размещению» `placement_id`; кто — тот,
    кто сменил размещение.
    """
    with (
        transaction.atomic(),
        stamped(source=StatusSource.PLACEMENT, placement_id=placement_id),
    ):
        changed = ProductSite.objects.filter(
            site_id=site_id, product_id=product_id, status__in=replaceable(target, fact=True)
        ).update(status=target, imported_undecided=False, updated_at=timezone.now())
    return changed > 0
