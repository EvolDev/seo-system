"""Админка блока 3: ключи продукта и их позиции.

Удаление отключено: ключ выключается снятием «активен», позиция — снапшот.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from django.utils import timezone

from apps.keywords.models import Keyword, KeywordPosition
from apps.workspace.products import WorkingProductFilter, product_filter
from config.admin import NoDeleteAdmin, SnapshotAdmin


@admin.register(Keyword)
class KeywordAdmin(NoDeleteAdmin):
    panel = True
    list_display = ("keyword", "product", "tool", "page_type", "volume", "anchor_type", "is_active")
    list_filter = (WorkingProductFilter, "tool", "is_active", "anchor_type")
    # Поиск нужен и выбору ключа у ссылки размещения.
    search_fields = ("keyword",)
    list_select_related = ("product",)


@admin.register(KeywordPosition)
class KeywordPositionAdmin(SnapshotAdmin):
    """Позиция — снапшот по ключу и стране.

    В отличие от снапшотов площадки дату можно указать: позиция одна на
    ключ, страну и дату, и та же дата второй раз — ошибка формы, а не
    дубль. По умолчанию в форме сегодняшняя дата.
    """

    list_display = ("keyword", "position", "country", "source", "checked_at")
    list_filter = (product_filter("keyword__product"), "country", "source")
    search_fields = ("keyword__keyword",)
    list_select_related = ("keyword",)
    autocomplete_fields = ("keyword",)
    snapshot_key = ("keyword_id", "country")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return ()

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, Any]:
        return {"checked_at": timezone.localdate(), **super().get_changeform_initial_data(request)}
