"""Админка блока 3: анкоры продукта, ключи и их позиции.

Ключ выключается флажком «активен», кнопкой в строке или массовым действием;
исторические ссылки и позиции сохраняются. Позиция — снапшот.

«Анкоры» (E3-05, ADR-059) — справочник по продукту: всё, что было на листе
«Распределение анкоров», плюс доли «цель / факт» по типам страниц, безанкорка
и страны над списком. Список — строки `v_keyword_coverage`; анкор правится
панелью справа (форма ключа), новый — окном «＋ Новый анкор», доли — прямо в
таблице над списком.

Страница показывает один список анкоров — регион (ADR-075): выбор с флагами,
последний запоминается. Позиции — по выдаче страны списка. Список удаляется
целиком со служебной страницы списка — с подтверждением, что уйдёт с ним.
"""

import datetime as dt
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote

from django.contrib import admin, messages
from django.contrib.admin.views.main import ChangeList
from django.db.models import F, OuterRef, QuerySet, Subquery
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_POST

from apps.keywords import anchor_export, anchor_views
from apps.keywords.models import (
    NAKED_TYPES,
    AnchorList,
    AnchorType,
    Keyword,
    KeywordCoverage,
    KeywordPosition,
)
from apps.sites import countries
from apps.sites.admin import ProductFilter
from apps.workspace.filters import Remembering, remembered
from apps.workspace.products import WorkingProductFilter, product_filter, working_product_id
from apps.workspace.saved_filters import screen_of
from config.admin import RecordAdmin, SnapshotAdmin
from config.assets import Css, Js

# Колонок позиций в списке «Анкоров» — как на листе таблицы: четыре последних снимка.
POSITION_COLUMNS = 4
_DATES = "_seo_anchor_dates"
_LIST = "_seo_anchor_list"
# Параметр адреса списка анкоров и экран, за которым запоминается выбор.
LIST_PARAM = "list"
SCREEN = screen_of("keywords", "keywordcoverage")


@admin.register(Keyword)
class KeywordAdmin(RecordAdmin):
    """Ключ (анкор) — форма для панели «Анкоров». Список ключей — «Анкоры»."""

    panel = True
    list_display = (
        "keyword",
        "product",
        "anchor_list",
        "tool",
        "page_type",
        "volume",
        "anchor_type",
        "is_active",
    )
    list_filter = (WorkingProductFilter, "anchor_list", "tool", "is_active", "anchor_type")
    # Поиск нужен и выбору ключа у ссылки размещения.
    search_fields = ("keyword",)
    list_select_related = ("product", "anchor_list__product")
    # Список — регион анкора (ADR-075): меняет его загрузка с другим регионом, не форма.
    readonly_fields = ("anchor_list",)
    fields = (
        "product",
        "anchor_list",
        "keyword",
        "target_url",
        "anchor_type",
        "page_type",
        "tool",
        "volume",
        "global_volume",
        "share",
        "is_active",
    )


@admin.register(AnchorList)
class AnchorListAdmin(RecordAdmin):
    """Список анкоров региона (ADR-075): заводит загрузка файла анкоров, здесь —
    посмотреть и удалить целиком. Подтверждение показывает, что уйдёт с ним:
    анкоры и их позиции; ссылки размещений останутся без анкора, размещения —
    без списка (регион снова угадается)."""

    panel = True
    list_display = ("product", "region_cell", "anchors_cell", "created_at")
    list_filter = (WorkingProductFilter,)
    list_select_related = ("product",)
    fields = ("product", "region_cell", "anchors_cell", "created_at")
    readonly_fields = ("product", "region_cell", "anchors_cell", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.display(description="регион")
    def region_cell(self, obj: AnchorList) -> SafeString:
        return format_html("{}{}", countries.flag_html(obj.country), obj.region_name)

    @admin.display(description="анкоров")
    def anchors_cell(self, obj: AnchorList) -> int:
        return obj.keywords.count()


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


class KindFilter(admin.SimpleListFilter):
    """Ключи или безанкорка."""

    title = "вид анкора"
    parameter_name = "kind"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("keys", "Ключи"), ("naked", "Безанкорные")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        naked = [str(kind) for kind in NAKED_TYPES]
        if self.value() == "keys":
            return queryset.exclude(anchor_type__in=naked)
        if self.value() == "naked":
            return queryset.filter(anchor_type__in=naked)
        return queryset


class ActivityFilter(admin.SimpleListFilter):
    title = "активность"
    parameter_name = "activity"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("active", "Активные"), ("inactive", "Неактивные"), ("all", "Все")]

    def choices(self, changelist: Any) -> Iterator[Any]:
        for value, label in self.lookup_choices:
            yield {
                "selected": (self.value() or "active") == value,
                "query_string": changelist.get_query_string({self.parameter_name: value}),
                "display": label,
            }

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.value() == "all":
            return queryset
        return queryset.filter(is_active=self.value() != "inactive")


class CoverageFilter(admin.SimpleListFilter):
    """Сколько ссылок: без ссылок, есть ждущие, есть размещённые."""

    title = "ссылки"
    parameter_name = "links"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("none", "Без ссылок"), ("waiting", "Есть ждущие"), ("placed", "Есть размещённые")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        if value == "none":
            return queryset.filter(links_placed=0, links_waiting=0)
        if value == "waiting":
            return queryset.filter(links_waiting__gt=0)
        if value == "placed":
            return queryset.filter(links_placed__gt=0)
        return queryset


class ValueFilter(admin.SimpleListFilter):
    """Значения колонки у анкоров выбранного продукта: Tool или тип страницы."""

    column = "tool"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        product = anchors_product(request)
        chosen, _ = anchors_lists(request)
        rows = KeywordCoverage.objects.exclude(**{f"{self.column}__isnull": True})
        if product is not None:
            rows = rows.filter(product_id=product)
        if chosen is not None:
            rows = rows.filter(anchor_list_id=chosen.pk)
        values = rows.order_by(self.column).values_list(self.column, flat=True).distinct()
        return [(value, value) for value in values]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        return queryset.filter(**{self.column: value}) if value else queryset


class ToolFilter(ValueFilter):
    title = "раздел сайта (Tool)"
    parameter_name = "tool"
    column = "tool"


class PageTypeFilter(ValueFilter):
    title = "тип страницы (Type)"
    parameter_name = "type"
    column = "page_type"


def anchors_product(request: HttpRequest) -> int | None:
    """Продукт экрана «Анкоры»: из адреса, иначе рабочий."""
    value = request.GET.get(ProductFilter.parameter_name, "")
    if value.isdigit():
        return int(value)
    return working_product_id(request)


def anchors_lists(request: HttpRequest) -> tuple[AnchorList | None, list[AnchorList]]:
    """Список анкоров экрана и все списки его продукта (ADR-075).

    Из адреса, иначе выбранный тут в прошлый раз, иначе основной — если он у
    продукта экрана: сменили продукт — открывается основной список нового.
    """
    if not hasattr(request, _LIST):
        product = anchors_product(request)
        lists = AnchorList.of_product(product) if product is not None else []
        by_id = {str(item.pk): item for item in lists}
        wanted = request.GET.get(LIST_PARAM) or remembered(request, SCREEN, LIST_PARAM) or ""
        found = by_id.get(wanted) or (lists[0] if lists else None)
        setattr(request, _LIST, (found, lists))
    chosen: tuple[AnchorList | None, list[AnchorList]] = getattr(request, _LIST)
    return chosen


class AnchorListFilter(Remembering, admin.SimpleListFilter):
    """Список анкоров — регион (ADR-075). Пункта «все» нет: доли считаются по одному
    списку. Без выбора — выбранный в прошлый раз, иначе основной (`anchors_lists`)."""

    title = "список анкоров"
    parameter_name = LIST_PARAM
    template = "admin/keywords/anchor_list_filter.html"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        _, self.lists = anchors_lists(request)
        return [(str(item.pk), item.region_name) for item in self.lists]

    def value(self) -> str | None:
        chosen, _ = anchors_lists(self.request)
        if chosen is None:
            return None
        value = str(chosen.pk)
        self.used_parameters[self.parameter_name] = value
        return value

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        if value is None:
            # Списков у продукта нет — и анкоров нет: продукт сузит до пустого.
            return queryset
        self.keep(value)
        return queryset.filter(anchor_list_id=int(value))

    def choices(self, changelist: Any) -> Iterator[Any]:
        value = self.value()
        for item in self.lists:
            yield {
                "selected": str(item.pk) == value,
                "query_string": changelist.get_query_string({self.parameter_name: item.pk}),
                "display": item.region_name,
                "flag": countries.flag_code(item.country) or "",
            }


@admin.register(KeywordCoverage)
class KeywordCoverageAdmin(RecordAdmin):
    """Анкоры продукта: лист «Распределение анкоров» и доли над ним (E3-05)."""

    list_display_links = None
    list_filter = (
        ProductFilter,
        AnchorListFilter,
        ActivityFilter,
        KindFilter,
        ToolFilter,
        PageTypeFilter,
        CoverageFilter,
    )
    actions = ("deactivate_selected", "activate_selected")
    search_fields = ("keyword", "target_url")
    ordering = ("id",)
    list_per_page = 200
    change_list_template = "admin/keywords/keywordcoverage/change_list.html"

    class Media:
        # offers.css и флаги — выбор списка с флагами (seo/country-picker.js), как «Регион».
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            "all": (Css("seo/offers.css"), Css("flags/sprite-hq.css"), Css("seo/anchors.css"))
        }
        js = (Js("seo/country-picker.js"), Js("seo/anchors.js"), Js("seo/anchor-export.js"))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    # «Удалить отмеченные» удаляет сами ключи — с позициями; ссылки остаются без ключа (ADR-060).
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.has_perm("keywords.delete_keyword"))

    def delete_roots(self, objs: Iterable[Any]) -> dict[Any, list[int]]:
        return {Keyword: [row.pk for row in objs]}

    def has_activate_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.has_perm("keywords.change_keyword"))

    @admin.action(description="Сделать выбранные анкоры неактивными", permissions=["activate"])
    def deactivate_selected(
        self, request: HttpRequest, queryset: QuerySet[KeywordCoverage]
    ) -> None:
        changed = Keyword.objects.filter(pk__in=queryset.values("pk"), is_active=True).update(
            is_active=False
        )
        self.message_user(request, f"Отключено анкоров: {changed}.", messages.SUCCESS)

    @admin.action(description="Сделать выбранные анкоры активными", permissions=["activate"])
    def activate_selected(self, request: HttpRequest, queryset: QuerySet[KeywordCoverage]) -> None:
        changed = Keyword.objects.filter(pk__in=queryset.values("pk"), is_active=False).update(
            is_active=True
        )
        self.message_user(request, f"Включено анкоров: {changed}.", messages.SUCCESS)

    def get_queryset(self, request: HttpRequest) -> QuerySet[KeywordCoverage]:
        queryset = super().get_queryset(request)
        country = positions_country(request)
        for index, date in enumerate(position_dates(request)):
            positions = KeywordPosition.objects.filter(
                keyword_id=OuterRef("pk"), country=country, checked_at=date
            ).values("position")[:1]
            queryset = queryset.annotate(**{f"pos_{index}": Subquery(positions)})
        return queryset

    def get_list_display(self, request: HttpRequest) -> list[Any]:
        columns: list[Any] = [
            "anchor_cell",
            _activity_column(request),
            "url_cell",
            "volume_cell",
            "global_cell",
            "placed_cell",
            "waiting_cell",
        ]
        dates = position_dates(request)
        columns += [_position_column(index, date) for index, date in enumerate(dates)]
        columns += ["tool", "page_type", "kind_cell"]
        return columns

    @admin.display(description="анкор", ordering="keyword")
    def anchor_cell(self, obj: KeywordCoverage) -> SafeString:
        url = reverse("admin:keywords_keyword_change", args=[obj.pk])
        return format_html(
            '<a href="{}" data-panel class="seo-anchor-name">{}</a>', url, obj.keyword
        )

    @admin.display(description="URL", ordering="target_url")
    def url_cell(self, obj: KeywordCoverage) -> SafeString:
        path_part = obj.target_url.split("://", 1)[-1]
        path_part = path_part[path_part.find("/") :] if "/" in path_part else "/"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer" class="seo-anchor-url"'
            ' title="{}">{}</a>',
            obj.target_url,
            obj.target_url,
            path_part,
        )

    @admin.display(description="Volume", ordering=F("volume").desc(nulls_last=True))
    def volume_cell(self, obj: KeywordCoverage) -> str:
        return _number(obj.volume)

    @admin.display(description="Global", ordering=F("global_volume").desc(nulls_last=True))
    def global_cell(self, obj: KeywordCoverage) -> str:
        return _number(obj.global_volume)

    @admin.display(description="Placed", ordering="-links_placed")
    def placed_cell(self, obj: KeywordCoverage) -> SafeString:
        return _count_html(obj.links_placed, "seo-anchor-placed")

    @admin.display(description="Waiting", ordering="-links_waiting")
    def waiting_cell(self, obj: KeywordCoverage) -> SafeString:
        return _count_html(obj.links_waiting, "seo-anchor-waiting")

    @admin.display(description="вид", ordering="anchor_type")
    def kind_cell(self, obj: KeywordCoverage) -> SafeString | str:
        if obj.anchor_type in {str(kind) for kind in NAKED_TYPES}:
            label = AnchorType(obj.anchor_type).label
            share = f" · {_pct(obj.share)}" if obj.share is not None else ""
            return format_html('<span class="seo-chip">{}{}</span>', label.lower(), share)
        return ""

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        product = anchors_product(request)
        chosen, lists = anchors_lists(request)
        page = anchor_views.page_context(request, product, chosen, lists)
        query = request.GET.urlencode()
        suffix = f"?{query}" if query else ""
        context = {
            **(extra_context or {}),
            **page,
            "anchor_copy_url": reverse("admin:keywords_keywordcoverage_copy") + suffix,
            "anchor_export_url": reverse("admin:keywords_keywordcoverage_export") + suffix,
        }
        if page["anchors_page"] is not None:
            # Заголовок экрана — с продуктом и регионом: «Анкоры Convertio · Бразилия».
            region = f" · {chosen.region_name}" if chosen is not None else ""
            context["title"] = f"Анкоры {page['anchors_page']['product'].name}{region}"
        return super().changelist_view(request, context)

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        match = request.resolver_match
        if match and match.url_name in {
            "keywords_keywordcoverage_copy",
            "keywords_keywordcoverage_export",
        }:
            return anchor_export.AnchorTransferChangeList
        return super().get_changelist(request, **kwargs)

    def copy_view(self, request: HttpRequest) -> HttpResponse:
        return anchor_export.copy_view(self, request)

    def excel_view(self, request: HttpRequest) -> HttpResponse:
        return anchor_export.excel_view(self, request)

    def get_urls(self) -> list[URLPattern]:
        view = self.admin_site.admin_view
        own = [
            path("copy/", view(require_POST(self.copy_view)), name="keywords_keywordcoverage_copy"),
            path(
                "export/",
                view(require_GET(self.excel_view)),
                name="keywords_keywordcoverage_export",
            ),
            path(
                "activity/<int:keyword_id>/",
                view(require_POST(anchor_views.activity_view)),
                name="keywords_anchor_activity",
            ),
            path(
                "new/",
                view(require_POST(anchor_views.quick_add_view)),
                name="keywords_anchor_add",
            ),
            path(
                "match/",
                view(require_GET(anchor_views.match_view)),
                name="keywords_anchor_match",
            ),
            path(
                "share/",
                view(require_POST(anchor_views.share_view)),
                name="keywords_anchor_share",
            ),
            path(
                "summary/<int:product_id>/",
                view(require_GET(anchor_views.summary_view)),
                name="keywords_anchor_summary",
            ),
        ]
        return own + super().get_urls()


def positions_country(request: HttpRequest) -> str:
    """Страна позиций в списке: страна выдачи списка экрана, как пишет загрузка (US, BR)."""
    chosen, _ = anchors_lists(request)
    return chosen.country.upper() if chosen is not None else "US"


def position_dates(request: HttpRequest) -> list[dt.date]:
    """Даты последних снимков позиций анкоров списка, новые слева — как на листе."""
    if not hasattr(request, _DATES):
        chosen, _ = anchors_lists(request)
        dates: list[dt.date] = []
        if chosen is not None:
            dates = list(
                KeywordPosition.objects.filter(
                    keyword__anchor_list_id=chosen.pk, country=positions_country(request)
                )
                .order_by("-checked_at")
                .values_list("checked_at", flat=True)
                .distinct()[:POSITION_COLUMNS]
            )
        setattr(request, _DATES, dates)
    found: list[dt.date] = getattr(request, _DATES)
    return found


def _position_column(index: int, date: dt.date) -> Any:
    def cell(obj: KeywordCoverage) -> SafeString | str:
        value = getattr(obj, f"pos_{index}", None)
        if value is None:
            return ""
        return format_html('<span class="seo-anchor-pos">{}</span>', value)

    cell.short_description = f"Pos {date:%d.%m}"  # type: ignore[attr-defined]
    cell.admin_order_field = f"pos_{index}"  # type: ignore[attr-defined]
    cell.__name__ = f"pos_{index}"
    return cell


def _number(value: int | None) -> str:
    return "" if value is None else f"{value:,}".replace(",", " ")


def _count_html(value: int, css: str) -> SafeString:
    if not value:
        return format_html('<span class="seo-anchor-zero">{}</span>', "—")
    return format_html('<span class="{}">{}</span>', css, value)


def _pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value.normalize():f}".replace(".", ",") + "%"


def _activity_column(request: HttpRequest) -> Any:
    editable = request.user.has_perm("keywords.change_keyword")

    def cell(obj: KeywordCoverage) -> SafeString:
        label = "Активен" if obj.is_active else "Неактивен"
        if not editable:
            return format_html('<span class="seo-sub">{}</span>', label)
        url = reverse("admin:keywords_anchor_activity", args=[obj.pk])
        return format_html(
            '<span class="seo-sub">{}</span> <button type="submit" name="active" value="{}" '
            'formaction="{}?next={}" formmethod="post" class="seo-btn seo-btn-small" '
            "data-no-panel>{}</button>",
            label,
            "0" if obj.is_active else "1",
            url,
            quote(request.get_full_path(), safe=""),
            "Отключить" if obj.is_active else "Включить",
        )

    cell.short_description = "Активность"  # type: ignore[attr-defined]
    cell.__name__ = "activity_cell"
    return cell
