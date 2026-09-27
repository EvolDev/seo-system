"""Админка блока 1: продукты, площадки, решения по ним и снапшоты.

Удаление отключено везде: ничего не удаляем физически. Площадку
скрывает пометка «удалена», решение по ней меняется статусом.
"""

from collections.abc import Iterator
from contextlib import suppress
from typing import Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from apps.sites.domains import normalize_domain
from apps.sites.models import (
    GrayScan,
    Product,
    ProductSite,
    Site,
    SiteAudit,
    SiteMetric,
    SitePrice,
)
from config.admin import NoDeleteAdmin, SnapshotAdmin


@admin.register(Product)
class ProductAdmin(NoDeleteAdmin):
    list_display = ("name", "domain", "is_active", "created_at")
    readonly_fields = ("created_at",)


class DeletedFilter(admin.SimpleListFilter):
    """По умолчанию — только площадки без пометки «удалена»."""

    title = "удалённые"
    parameter_name = "deleted"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Только удалённые"), ("all", "Все, включая удалённые")]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        if self.value() == "yes":
            return queryset.filter(is_deleted=True)
        if self.value() == "all":
            return queryset
        return queryset.filter(is_deleted=False)

    def choices(self, changelist: Any) -> Iterator[Any]:
        # Первый пункт Django называет «Все», но без фильтра у нас — активные.
        choices = super().choices(changelist)
        yield {**next(choices), "display": "Активные"}
        yield from choices


class ProductSiteInline(admin.TabularInline):  # type: ignore[type-arg]
    """Статус площадки по каждому продукту. Строки создаёт система."""

    model = ProductSite
    fields = ("product", "status", "reject_reason", "imported_undecided")
    readonly_fields = ("product", "imported_undecided")
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Site)
class SiteAdmin(NoDeleteAdmin):
    list_display = ("domain", "language", "link_type", "marks_as_ad", "is_deleted")
    list_filter = (DeletedFilter, "language", "link_type", "marks_as_ad")
    search_fields = ("domain",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (ProductSiteInline,)

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Site]:
        # Менеджер по умолчанию прячет удалённые; здесь их скрывает
        # DeletedFilter, чтобы пометку можно было снять.
        return Site.all_objects.all()

    def get_search_results(
        self, request: HttpRequest, queryset: models.QuerySet[Site], search_term: str
    ) -> tuple[models.QuerySet[Site], bool]:
        # Человек вставит адрес целиком: https://www.example.com/article.
        # Не домен (например, пустой запрос) — ищем как есть.
        with suppress(ValueError):
            search_term = normalize_domain(search_term)
        return super().get_search_results(request, queryset, search_term)


@admin.register(ProductSite)
class ProductSiteAdmin(NoDeleteAdmin):
    list_display = ("site", "product", "status", "imported_undecided", "updated_at")
    list_filter = ("product", "status", "imported_undecided")
    search_fields = ("site__domain",)
    readonly_fields = ("site", "product", "created_at", "updated_at")
    list_select_related = ("site", "product")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


class SiteSnapshotAdmin(SnapshotAdmin):
    """Снапшоты площадки: последний — среди замеров этой площадки."""

    snapshot_key: tuple[str, ...] = ("site_id",)
    autocomplete_fields = ("site",)


@admin.register(SiteMetric)
class SiteMetricAdmin(SiteSnapshotAdmin):
    list_display = ("site", "dr", "organic_traffic", "total_keywords", "source", "checked_at")
    list_filter = ("source",)
    search_fields = ("site__domain",)
    list_select_related = ("site",)


@admin.register(SitePrice)
class SitePriceAdmin(SiteSnapshotAdmin):
    list_display = (
        "site",
        "placement_cents",
        "announce_cents",
        "writing_cents",
        "currency",
        "checked_at",
    )
    list_filter = ("source",)
    search_fields = ("site__domain",)
    list_select_related = ("site",)


@admin.register(GrayScan)
class GrayScanAdmin(SiteSnapshotAdmin):
    list_display = ("site", "ratio", "gray_hits", "total_indexed", "method", "checked_at")
    search_fields = ("site__domain",)
    list_select_related = ("site",)


@admin.register(SiteAudit)
class SiteAuditAdmin(SiteSnapshotAdmin):
    list_display = ("site", "product", "verdict", "score", "author", "created_at")
    list_filter = ("product", "verdict", "author")
    search_fields = ("site__domain",)
    list_select_related = ("site", "product")
    snapshot_key = ("site_id", "product_id")
    time_field = "created_at"
