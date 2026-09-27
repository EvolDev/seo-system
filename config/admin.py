"""Базовые классы админки для всех доменных приложений.

Удаление отключено везде: ничего не удаляем физически (ADR-008).
Инфраструктура для моделей всех приложений, поэтому живёт в `config/`,
как `config/db.py`.
"""

from typing import Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest


class NoDeleteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class SnapshotAdmin(NoDeleteAdmin):
    """Снапшоты (ADR-008, уточнение 27.09.2026).

    Новый замер — «Сохранить как новый объект»: форма открывается с
    данными последнего снапшота, сохранение создаёт новую строку. Править
    можно только последний снапшот, более старые — только просмотр. Дата
    замера в форме не редактируется: у нового снапшота она своя.
    """

    save_as = True
    # Колонки, внутри которых снапшот «последний»: у метрик — площадка
    # (`site_id`), у аудита — площадка и продукт. Задаёт наследник.
    snapshot_key: tuple[str, ...]
    time_field = "checked_at"

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return (self.time_field,)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if obj is not None and not self.is_latest(obj):
            return False
        return super().has_change_permission(request, obj)

    def is_latest(self, obj: models.Model) -> bool:
        key = {column: getattr(obj, column) for column in self.snapshot_key}
        latest = (
            type(obj)
            ._default_manager.filter(**key)
            .order_by(f"-{self.time_field}", "-pk")
            .values_list("pk", flat=True)
            .first()
        )
        return bool(latest == obj.pk)
