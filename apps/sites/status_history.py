"""История статусов на экранах (E1-13, ADR-049): карточка площадки и размещение.

Строки пишет триггер в базе (`site_status_changes`, `placement_status_changes`),
здесь — только показ: когда, с какого статуса на какой, кто и откуда.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.placements.models import PlacementStatus, PlacementStatusChange
from apps.sites.models import SiteStatus, SiteStatusChange, StatusSource


@dataclass(frozen=True)
class HistoryRow:
    when: str
    change: str
    who: SafeString | str


def site_history(site_id: int) -> dict[int, list[HistoryRow]]:
    """Смены статуса площадки по продуктам, новые сверху: `{product_site_id: [...]}`.

    Один запрос на всю карточку.
    """
    changes = (
        SiteStatusChange.objects.filter(product_site__site_id=site_id)
        .select_related("actor")
        .order_by("-changed_at", "-pk")
    )
    rows: dict[int, list[HistoryRow]] = defaultdict(list)
    for change in changes:
        rows[change.product_site_id].append(
            HistoryRow(
                when=_when(change.changed_at),
                change=_change(change.from_status, change.to_status, SiteStatus, "Создана"),
                who=_who(change.source, change.actor, placement_id=change.placement_id),
            )
        )
    return dict(rows)


def placement_history(placement_id: int) -> list[HistoryRow]:
    """Смены статуса размещения, новые сверху."""
    changes = (
        PlacementStatusChange.objects.filter(placement_id=placement_id)
        .select_related("actor")
        .order_by("-changed_at", "-pk")
    )
    return [
        HistoryRow(
            when=_when(change.changed_at),
            change=_change(change.from_status, change.to_status, PlacementStatus, "Создано"),
            who=_who(change.source, change.actor),
        )
        for change in changes
    ]


def _when(moment: Any) -> str:
    return f"{timezone.localtime(moment):%d.%m.%Y %H:%M}"


def _change(
    before: str | None, after: str, statuses: type[SiteStatus] | type[PlacementStatus], first: str
) -> str:
    """«Просмотрено → Заявка отправлена»; без прежнего — «Создано: Запланировано»."""
    if before is None:
        return f"{first}: {statuses(after).label}"
    return f"{statuses(before).label} → {statuses(after).label}"


def _who(source: str | None, actor: Any, *, placement_id: int | None = None) -> SafeString | str:
    """Кто и откуда: «Алиса, панель», «импорт таблицы», «по размещению · Алиса»."""
    name = (actor.get_full_name() or actor.get_username()) if actor is not None else None
    if source == StatusSource.PLACEMENT:
        # Ссылка — размещение в той же панели (ADR-048).
        what: SafeString | str = (
            format_html(
                '<a href="{}" data-panel>по размещению</a>',
                reverse("admin:placements_placement_change", args=[placement_id]),
            )
            if placement_id is not None
            else StatusSource.PLACEMENT.label
        )
        return format_html("{} · {}", what, name) if name else what
    label = StatusSource(source).label if source else None
    if name and label:
        return f"{name}, {label}"
    return name or label or "не отмечено"
