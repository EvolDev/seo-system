"""Экран «Ссылающиеся домены» — кто ссылается на продукт по выгрузкам Ahrefs (E1-09, ADR-051).

Только просмотр: список пополняет загрузка «Ссылающиеся домены Ahrefs». Рядом с
доменом — площадка базы с тем же доменом, если она есть: её карточка открывается
панелью.
"""

from typing import Any

from django.contrib import admin
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.sites.models import ProductRefDomain, Site
from apps.sites.upload_admin import card_url
from config.admin import NoDeleteAdmin


def _day(value: Any) -> str:
    return f"{value:%d.%m.%Y}" if value else ""


class StateFilter(admin.SimpleListFilter):
    """Ссылается сейчас или ссылка пропала: дата Lost у Ahrefs или нет в новой выгрузке."""

    title = "состояние"
    parameter_name = "state"
    LINKING, LOST = "linking", "lost"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [(self.LINKING, "Ссылается"), (self.LOST, "Ссылка пропала")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        linking = Q(lost_at__isnull=True, missing_since__isnull=True)
        if self.value() == self.LINKING:
            return queryset.filter(linking)
        if self.value() == self.LOST:
            return queryset.exclude(linking)
        return queryset


@admin.register(ProductRefDomain)
class ProductRefDomainAdmin(NoDeleteAdmin):
    list_display = ("domain", "product", "state_cell", "first_seen_at", "seen_on", "site_cell")
    list_display_links = None
    list_filter = ("product", StateFilter)
    search_fields = ("domain",)
    ordering = ("domain",)
    list_select_related = ("product",)
    list_per_page = 100

    def get_queryset(self, request: HttpRequest) -> QuerySet[ProductRefDomain]:
        queryset: QuerySet[ProductRefDomain] = super().get_queryset(request)
        site = Site.objects.filter(domain=OuterRef("domain")).values("pk")[:1]
        return queryset.annotate(site_id=Subquery(site))

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Список пополняет только загрузка выгрузки Ahrefs.
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="состояние")
    def state_cell(self, obj: ProductRefDomain) -> SafeString:
        if obj.lost_at is not None:
            return format_html(
                '<span class="seo-chip seo-up">пропала {}</span> <span class="seo-sub">{}</span>',
                _day(obj.lost_at),
                "по Ahrefs",
            )
        if obj.missing_since is not None:
            return format_html(
                '<span class="seo-chip seo-up">нет в выгрузке с {}</span>', _day(obj.missing_since)
            )
        return format_html('<span class="seo-chip seo-down">{}</span>', "ссылается")

    @admin.display(description="площадка в базе")
    def site_cell(self, obj: ProductRefDomain) -> SafeString | str:
        site_id = getattr(obj, "site_id", None)
        if site_id is None:
            return "—"
        return format_html('<a href="{}" data-panel>карточка</a>', card_url(site_id))
