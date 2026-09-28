"""Админка блока 2: размещения и ссылки в них.

Удаление отключено: размещение отменяется статусом «Отменено», ссылка
исправляется, но не удаляется.
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import StackedInline

from apps.placements.models import Placement, PlacementLink
from config.admin import NoDeleteAdmin


class PlacementLinkInline(StackedInline):
    """Ссылки размещения: задание правит человек, остальное — проверка страницы.

    Что на странице (`rel`, позиция, живость, время пропажи), пишут
    краулер и проверка живости (E2-04, E2-05) — в форме только чтение:
    время пропажи пишется один раз. Тест на извлечение до E7-02 делает
    человек.
    """

    model = PlacementLink
    can_delete = False
    autocomplete_fields = ("keyword",)
    readonly_fields = (
        "is_alive",
        "rel",
        "char_offset",
        "char_offset_no_spaces",
        "context_sentence",
        "first_seen_at",
        "last_checked_at",
        "lost_at",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    ("anchor", "anchor_type"),
                    "target_url",
                    ("keyword", "link_index"),
                    "extraction_test_passed",
                )
            },
        ),
        (
            "На странице — заполняет проверка",
            {
                "classes": ("collapse",),
                "fields": (
                    ("is_alive", "rel"),
                    ("char_offset", "char_offset_no_spaces"),
                    "context_sentence",
                    ("first_seen_at", "last_checked_at", "lost_at"),
                ),
            },
        ),
    )

    def get_extra(self, request: HttpRequest, obj: Any = None, **kwargs: Any) -> int:
        # У нового размещения сразу два слота: ссылок в статье одна-две.
        return 2 if obj is None else 0


@admin.register(Placement)
class PlacementAdmin(NoDeleteAdmin):
    list_display = ("site", "product", "status", "placement_type", "published_at", "is_indexed")
    list_filter = ("product", "status", "placement_type", "is_indexed")
    search_fields = ("site__domain", "article_url", "collaborator_order_id")
    list_select_related = ("site", "product")
    autocomplete_fields = ("site",)
    readonly_fields = ("run_id", "created_at", "updated_at")
    inlines = (PlacementLinkInline,)
