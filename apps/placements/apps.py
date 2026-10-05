from collections.abc import Iterable

from django.apps import AppConfig
from django.db import models


class PlacementsConfig(AppConfig):
    name = "apps.placements"
    verbose_name = "Размещения"

    def ready(self) -> None:
        from config import deletion

        from .models import Placement

        # Проверки индексации ссылаются на размещение без внешнего ключа
        # (журнал полиморфный): удалили размещение — уходит и его журнал (ADR-060).
        deletion.register_dependents(Placement, _placement_checks)


def _placement_checks(ids: list[int]) -> tuple[type[models.Model], Iterable[int]]:
    from apps.observability.models import Check

    from .indexation import ENTITY_TYPE

    found = Check.objects.filter(entity_type=ENTITY_TYPE, entity_id__in=ids)
    return Check, found.values_list("pk", flat=True)
