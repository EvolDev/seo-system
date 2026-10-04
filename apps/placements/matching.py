"""Какое размещение продукта на площадке дополнять — общее правило загрузок (ADR-051).

Естественного ключа у размещения нет. Импорт таблицы (маппинг §1.5–1.6) и
загрузка размещений (E1-09) ищут его одинаково — среди размещений этого
продукта на этой площадке:

- в строке есть адрес статьи — размещение с тем же адресом. Адреса
  сравниваются как у проверки индексации (`indexation.normalize_url`): без
  схемы, `www.`, `/` в конце, `#…` и меток отслеживания — у custify.com в
  таблице адрес с `#:~:text=…`, в файле коллеги без;
- иначе — единственное без адреса: адрес ему и достанется. Список одних
  доменов от продавца, а потом файл или таблица с адресом не дадут двух
  размещений;
- без адреса несколько — какое дополнять, неясно: строка в отчёт, ничего
  не трогается; ни одного — новое размещение.

Строка без адреса статьи — «на этой площадке размещались»: подходит и
единственное размещение продукта с адресом (`match_without_url`). Импорт
таблицы так не делает: заявка без адреса на площадке с вышедшей статьёй там —
новое размещение (маппинг §1.5).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from apps.placements.indexation import normalize_url
from apps.placements.models import Placement, PlacementStatus

# Статус размещения из файла — только вперёд по цепочке. Отклонённое и
# отменённое файл не трогает: так решил человек (маппинг §1.5).
LADDER = (
    PlacementStatus.PLANNED,
    PlacementStatus.ORDERED,
    PlacementStatus.WRITING,
    PlacementStatus.REVIEW,
    PlacementStatus.PUBLISHED,
)


def moves_forward(current: str, target: str) -> bool:
    """`target` дальше `current` по цепочке статусов размещения."""
    return current in LADDER and target in LADDER and LADDER.index(target) > LADDER.index(current)


@dataclass(frozen=True)
class Match:
    placement: Placement | None  # None и не `ambiguous` — создать новое
    ambiguous: bool = False  # подходит несколько: какое дополнять, неясно


NEW = Match(None)
AMBIGUOUS = Match(None, ambiguous=True)


def same_article(first: str, second: str) -> bool:
    """Один и тот же адрес статьи — с точностью до записи адреса."""
    return normalize_url(first) == normalize_url(second)


def match_placement(candidates: Sequence[Placement], article_url: str | None) -> Match:
    """Размещение для строки с адресом статьи (или без него — по правилу импорта)."""
    if article_url:
        target = normalize_url(article_url)
        for placement in candidates:
            if placement.article_url and normalize_url(placement.article_url) == target:
                return Match(placement)
    return _single_without_url(candidates)


def match_without_url(candidates: Sequence[Placement]) -> Match:
    """Строка без адреса: единственное размещение продукта, иначе единственное без адреса."""
    if len(candidates) == 1:
        return Match(candidates[0])
    if not candidates:
        return NEW
    without_url = [p for p in candidates if not p.article_url]
    return Match(without_url[0]) if len(without_url) == 1 else AMBIGUOUS


def _single_without_url(candidates: Sequence[Placement]) -> Match:
    without_url = [p for p in candidates if not p.article_url]
    if len(without_url) > 1:
        return AMBIGUOUS
    return Match(without_url[0]) if without_url else NEW
