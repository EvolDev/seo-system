"""Админка блока 1: продукты, площадки, решения по ним, снапшоты, списки.

Удаление отключено везде: ничего не удаляем физически. Площадку
скрывает пометка «удалена», решение по ней меняется статусом. Исключения —
локальные настройки на странице продукта (ADR-035) и продукт, с которым
ещё не работали (ADR-036).
"""

from collections.abc import Iterator
from contextlib import suppress
from typing import Any

from django.contrib import admin
from django.db import models
from django.db.models import Count, Q
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html

from apps.content.admin import (
    PRODUCT_SETTING_FIELDS,
    ProductOtherSettingsInline,
    ProductSettingsForm,
)
from apps.sites.domains import normalize_domain
from apps.sites.models import (
    GrayScan,
    Product,
    ProductSite,
    Site,
    SiteAudit,
    SiteList,
    SiteListItem,
    SiteMetric,
    SitePrice,
)
from config.admin import NoDeleteAdmin, SnapshotAdmin


@admin.register(Product)
class ProductAdmin(NoDeleteAdmin):
    """Продукт и его настройки: заводя продукт, человек сразу видит, что заполнить."""

    form = ProductSettingsForm
    list_display = ("name", "domain", "is_active", "created_at")
    readonly_fields = ("created_at",)
    fieldsets = (
        (None, {"fields": ("name", "domain", "is_active", "created_at")}),
        (
            "Настройки продукта",
            {
                "fields": PRODUCT_SETTING_FIELDS,
                "description": (
                    "Пустое поле — у продукта нет своего значения: действует общее из"
                    " раздела «Настройки», а где общего нет, признак не считается."
                ),
            },
        ),
    )
    inlines = (ProductOtherSettingsInline,)

    def save_model(
        self, request: HttpRequest, obj: Product, form: ProductSettingsForm, change: bool
    ) -> None:
        super().save_model(request, obj, form, change)
        form.save_settings(obj)

    # Продукт, заведённый по ошибке, удаляется, пока с ним не работали
    # (ADR-036). По одному, со страницы продукта: массового удаления нет.
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if obj is None or obj.has_history():
            return False
        return admin.ModelAdmin.has_delete_permission(self, request, obj)

    def get_deleted_objects(
        self, objs: Any, request: HttpRequest
    ) -> tuple[list[str], dict[str, int], set[str], list[str]]:
        # Строки продукт × площадка защищены от удаления (PROTECT), и штатная
        # страница подтверждения отказала бы. Удаляет их delete_unused.
        products = list(objs)
        counts = {
            "продукты": len(products),
            "площадки продуктов — пустые строки": sum(p.product_sites.count() for p in products),
            "настройки продукта": sum(p.domain_settings.count() for p in products),
        }
        return [str(product) for product in products], counts, set(), []

    def delete_model(self, request: HttpRequest, obj: Product) -> None:
        obj.delete_unused()


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


@admin.register(SiteList)
class SiteListAdmin(NoDeleteAdmin):
    """Рабочие списки (ADR-033). Создаёт их импорт; здесь — обзор и имя."""

    list_display = ("name", "sites_count", "first_seen_count", "source", "created_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "sites_count", "first_seen_count")

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[SiteList]:
        # Счётчики одним запросом на весь список, а не запросом на строку.
        queryset: models.QuerySet[SiteList] = super().get_queryset(request)
        return queryset.annotate(
            sites_total=Count("items"),
            first_seen_total=Count("items", filter=Q(items__first_seen=True)),
        )

    @admin.display(description="площадок", ordering="sites_total")
    def sites_count(self, obj: Any) -> str:
        url = reverse("admin:sites_sitelistitem_changelist") + f"?site_list__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, obj.sites_total)

    @admin.display(description="впервые в базе", ordering="first_seen_total")
    def first_seen_count(self, obj: Any) -> int:
        return int(obj.first_seen_total)


@admin.register(SiteListItem)
class SiteListItemAdmin(NoDeleteAdmin):
    """Площадки списка — только просмотр: в список их добавляет импорт."""

    list_display = ("site", "site_list", "first_seen", "added_at")
    list_filter = ("site_list", "first_seen")
    search_fields = ("site__domain",)
    list_select_related = ("site", "site_list")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
