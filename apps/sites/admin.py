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
from django.db.models import Count, Exists, F, OuterRef, Q
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from rangefilter.filters import NumericRangeFilter

from apps.content.admin import (
    PRODUCT_SETTING_FIELDS,
    ProductOtherSettingsInline,
    ProductSettingsForm,
)
from apps.placements.models import Placement
from apps.sites.domains import normalize_domain
from apps.sites.models import (
    GrayScan,
    Product,
    ProductSite,
    ProductSiteLatest,
    Site,
    SiteAudit,
    SiteList,
    SiteListItem,
    SiteMetric,
    SitePrice,
    SiteStatus,
)
from config.admin import ModelAdmin, NoDeleteAdmin, SnapshotAdmin, TabularInline


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
        return ModelAdmin.has_delete_permission(self, request, obj)

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


class ProductSiteInline(TabularInline):
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
        # Ведёт на рабочий экран «Площадки» с этим списком в фильтре, а не в
        # служебную таблицу строк списка — её нет в меню (E9-08).
        url = reverse("admin:sites_productsitelatest_changelist") + f"?list={obj.pk}"
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


# ---------- «Площадки»: рабочий экран поверх v_product_site_latest ----------


def _euros(cents: int | None) -> str:
    """Центы — в евро для показа: 12000 → «€120», 12050 → «€120.50»."""
    if cents is None:
        return ""
    euros, rest = divmod(cents, 100)
    return f"€{euros}" if rest == 0 else f"€{euros}.{rest:02d}"


class ProductFilter(admin.SimpleListFilter):
    """Продукт, чьими глазами смотрим на площадки. Пункта «все» нет.

    Без выбора — первый активный продукт: одна площадка у двух продуктов
    дала бы две строки с разными статусами (ADR-030).
    """

    title = "продукт"
    parameter_name = "product"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        products = Product.objects.order_by("pk").values_list("pk", "name")
        return [(str(pk), name) for pk, name in products]

    def value(self) -> str | None:
        value = super().value()
        if not value and self.lookup_choices:
            active = Product.objects.filter(is_active=True).order_by("pk")
            first = active.values_list("pk", flat=True).first()
            value = str(first) if first is not None else self.lookup_choices[0][0]
            # Запоминаем, чтобы не спрашивать базу второй раз при отрисовке.
            self.used_parameters[self.parameter_name] = value
        return str(value) if value is not None else None

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        if value is None:
            return queryset
        return queryset.filter(product_id=int(value)) if value.isdigit() else queryset.none()

    def choices(self, changelist: Any) -> Iterator[Any]:
        # Первый пункт Django — «Все»; у нас без выбора — продукт по умолчанию.
        choices = super().choices(changelist)
        next(choices)
        yield from choices


class SiteListFilter(admin.SimpleListFilter):
    """Рабочий список (ADR-033). Без выбора — самый новый список."""

    title = "список"
    parameter_name = "list"
    ALL = "all"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        lists = SiteList.objects.order_by("-created_at", "-pk").values_list("pk", "name")
        return [*((str(pk), name) for pk, name in lists), (self.ALL, "Все площадки")]

    def value(self) -> str | None:
        value = super().value()
        if not value:
            # Первый пункт — самый новый список, а если списков нет — «все».
            value = self.lookup_choices[0][0]
            self.used_parameters[self.parameter_name] = value
        return str(value)

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        if value == self.ALL:
            return queryset
        if value is None or not value.isdigit():
            return queryset.none()
        in_list = SiteListItem.objects.filter(site_list_id=int(value)).values("site_id")
        return queryset.filter(site_id__in=in_list)

    def choices(self, changelist: Any) -> Iterator[Any]:
        # «Все площадки» — свой пункт в конце списка, штатное «Все» не нужно.
        choices = super().choices(changelist)
        next(choices)
        yield from choices


class WorkedFilter(admin.SimpleListFilter):
    """«Уже работали / новые для нас» — по выбранному продукту (ADR-033).

    Уже работали: статус не «Новая», был аудит под продукт или есть
    размещение продукта в любом статусе. Размещение другого продукта сюда
    не входит — оно видно в колонке «другие продукты».
    """

    title = "уже работали"
    parameter_name = "worked"
    YES, NO = "yes", "no"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [(self.YES, "Уже работали"), (self.NO, "Новые для нас")]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        if self.value() not in (self.YES, self.NO):
            return queryset
        placed = Placement.objects.filter(
            site_id=OuterRef("site_id"), product_id=OuterRef("product_id")
        )
        worked = ~Q(status=SiteStatus.NEW) | Q(audited_at__isnull=False) | Exists(placed)
        return queryset.filter(worked) if self.value() == self.YES else queryset.exclude(worked)


class PublishedFilter(admin.SimpleListFilter):
    """Есть ли опубликованные размещения выбранного продукта."""

    title = "размещения"
    parameter_name = "published"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Есть опубликованные"), ("no", "Нет опубликованных")]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        if self.value() == "yes":
            return queryset.filter(placements_published__gt=0)
        if self.value() == "no":
            return queryset.filter(placements_published=0)
        return queryset


class LanguageFilter(admin.SimpleListFilter):
    """Основной язык площадки. Языков десятки — выпадающий список."""

    title = "язык"
    parameter_name = "language"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        languages = (
            Site.objects.exclude(language__isnull=True)
            .order_by("language")
            .values_list("language", flat=True)
            .distinct()
        )
        return [(str(language), str(language)) for language in languages]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        return queryset.filter(language=value) if value else queryset


@admin.register(ProductSiteLatest)
class ProductSiteLatestAdmin(NoDeleteAdmin):
    """Площадки продукта «на сегодня» — рабочий список вместо Excel.

    Только просмотр: строка — это представление. Статус меняется по
    ссылке в колонке «статус», факты о площадке — по ссылке на домене.
    Всё, что в колонках, приходит одним запросом из представления.
    """

    list_display = (
        "domain_link",
        "status_link",
        "dr",
        "organic_traffic",
        "language",
        "reference_price",
        "writing_price",
        "we_write",
        "expected_spend",
        "verdict",
        "placements_published",
        "other_products",
    )
    list_display_links = None
    list_filter = (
        ProductFilter,
        SiteListFilter,
        WorkedFilter,
        "status",
        # Диапазон — поля «С» и «До» (django-admin-rangefilter, ADR-038).
        ("dr", NumericRangeFilter),
        ("organic_traffic", NumericRangeFilter),
        LanguageFilter,
        "we_write",
        PublishedFilter,
    )
    search_fields = ("domain",)
    ordering = (F("dr").desc(nulls_last=True), "domain")
    list_per_page = 100
    # Полный счётчик без фильтров — лишний запрос на каждую страницу.
    show_full_result_count = False

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_search_results(
        self, request: HttpRequest, queryset: models.QuerySet[Any], search_term: str
    ) -> tuple[models.QuerySet[Any], bool]:
        # Как в «Площадках»: вставленный адрес целиком сводится к домену.
        with suppress(ValueError):
            search_term = normalize_domain(search_term)
        return super().get_search_results(request, queryset, search_term)

    @admin.display(description="домен", ordering="domain")
    def domain_link(self, obj: ProductSiteLatest) -> str:
        url = reverse("admin:sites_site_change", args=[obj.site_id])
        return format_html('<a href="{}">{}</a>', url, obj.domain)

    @admin.display(description="статус", ordering="status")
    def status_link(self, obj: ProductSiteLatest) -> str:
        url = reverse("admin:sites_productsite_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.get_status_display())

    @admin.display(description="размещение + анонс", ordering="reference_total_cents")
    def reference_price(self, obj: ProductSiteLatest) -> str:
        # Без замера цен представление даёт 0 (coalesce) — показываем пусто.
        return "" if obj.prices_at is None else _euros(obj.reference_total_cents)

    @admin.display(description="написание", ordering="writing_cents")
    def writing_price(self, obj: ProductSiteLatest) -> str:
        return _euros(obj.writing_cents)

    @admin.display(description="плановые расходы", ordering="expected_spend_cents")
    def expected_spend(self, obj: ProductSiteLatest) -> str:
        # Без замера цен сумма была бы 0, будто бесплатно, — пусто.
        return "" if obj.prices_at is None else _euros(obj.expected_spend_cents)

    @admin.display(description="аудит", ordering="audited_at")
    def verdict(self, obj: ProductSiteLatest) -> str:
        if obj.last_verdict is None:
            return ""
        label = obj.get_last_verdict_display()
        return label if obj.last_score is None else f"{label}, {obj.last_score}"

    @admin.display(description="другие продукты")
    def other_products(self, obj: ProductSiteLatest) -> str:
        # Статья другого продукта «уже работали» не делает, но её видно (ADR-033).
        if not obj.other_products_placed:
            return ""
        url = reverse("admin:placements_placement_changelist") + f"?site__id__exact={obj.site_id}"
        return format_html('<a href="{}">{}</a>', url, ", ".join(obj.other_products_placed))
