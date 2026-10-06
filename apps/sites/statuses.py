"""Как статус площадки у продукта меняется сам (E1-12, ADR-047, ADR-062).

Словарь статусов общий у площадки и размещения, поэтому статус размещения
переносится на площадку как есть. Путь в работу — лесенка «Новая →
Просмотрено → В работе → Заявка отправлена → Написание статьи → Размещено».
Система двигает статус по ней только вперёд: новая заявка «Размещено» не
откатывает.

Заявка, написание и публикация размещения — факты. Новый факт — размещение
только что создано или его статус только что сменился — сильнее прежнего
решения «Отбрасываю» и «Отказ». Старый факт, повторённый ещё раз (та же
заявка в таблице при следующем импорте), решение человека не перекрывает:
отказ мог быть поставлен уже после заявки. «Чёрный список» система не трогает
никогда. Решение без факта — «В работе» по запланированному размещению, отказ
из таблицы или аудита — встаёт только туда, где решения ещё нет: на «Новая» и
«Просмотрено».

Правила — только для системы: размещения (`Placement.save()`), импорта
таблицы, аудита (E4). Человек в окне статуса ставит любой статус.
"""

from django.db import transaction
from django.utils import timezone

from apps.sites.models import ProductSite, StatusSource, WorkStatus
from config.changes import stamped

LADDER = (
    WorkStatus.NEW,
    WorkStatus.VIEWED,
    WorkStatus.IN_WORK,
    WorkStatus.ORDERED,
    WorkStatus.WRITING,
    WorkStatus.PLACED,
)
# Решения ещё нет.
UNDECIDED = (WorkStatus.NEW, WorkStatus.VIEWED)
# Что ставит факт размещения: заявка, написание статьи и публикация.
FACTS = (WorkStatus.ORDERED, WorkStatus.WRITING, WorkStatus.PLACED)
# Прежние решения, которые новый факт перекрывает. Чёрного списка здесь нет.
OVERRIDDEN_BY_FACT = (WorkStatus.DISCARDED, WorkStatus.REJECTED)


def replaceable(target: WorkStatus, *, fact: bool = False) -> tuple[WorkStatus, ...]:
    """Статусы, которые система сама сменит на `target`.

    `fact` — `target` ставит новый факт: размещение только что создано или
    его статус только что сменился.
    """
    if target not in LADDER:
        return UNDECIDED
    behind = LADDER[: LADDER.index(target)]
    return behind + OVERRIDDEN_BY_FACT if fact and target in FACTS else behind


def advance(
    site_id: int, product_id: int, target: WorkStatus, *, placement_id: int | None = None
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
