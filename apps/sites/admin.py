"""Админка блока 1: продукты, площадки, решения, продавцы, цены, списки.

Удаление отключено везде: ничего не удаляем физически. Площадку
скрывает пометка «удалена», решение по ней меняется статусом. Исключения —
локальные настройки на странице продукта (ADR-035) и продукт, с которым
ещё не работали (ADR-036).

Рабочая цена, предложения продавцов, заметки и карточка площадки —
ADR-043; правила смены цены — `apps/sites/offers.py`.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import suppress
from typing import Any, ClassVar

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.views.main import ChangeList
from django.db import models
from django.db.models import Count, Exists, F, Max, OuterRef, Q, Subquery
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_POST
from rangefilter.filters import NumericRangeFilter

from apps.content.admin import (
    PRODUCT_SETTING_FIELDS,
    ProductOtherSettingsInline,
    ProductSettingsForm,
)
from apps.placements.models import Placement
from apps.sites import offers
from apps.sites.display import (
    Amount,
    announce_text,
    delta,
    delta_html,
    delta_text,
    price_html,
    round_euros,
    seller_mark,
    writing_html,
)
from apps.sites.domains import normalize_domain
from apps.sites.models import (
    ExchangeRate,
    GrayScan,
    PlacementType,
    Product,
    ProductSite,
    ProductSiteLatest,
    Seller,
    Site,
    SiteAudit,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteOffer,
    SitePrice,
    SiteStatus,
)
from config.admin import ModelAdmin, NoDeleteAdmin, SnapshotAdmin, TabularInline

# Окно карточки просит у сервера только её содержимое, без страницы вокруг.
PARTIAL_HEADER = "X-Seo-Partial"


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


def _note_rejection(request: HttpRequest, row: ProductSite, changed: list[str]) -> None:
    """Причина отказа, поставленная в админке, — сразу и в историю заметок (ADR-043)."""
    if "reject_reason" in changed and row.reject_reason:
        offers.add_note(row.site_id, row.reject_reason, product=row.product, author=request.user)


class ProductSiteInline(TabularInline):
    """Статус площадки по каждому продукту. Строки создаёт система."""

    model = ProductSite
    fields = ("product", "status", "reject_reason", "imported_undecided")
    readonly_fields = ("product", "imported_undecided")
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def _day(moment: dt.datetime | None) -> str:
    """Дата по времени проекта: в базе UTC, и 27.09 00:00 по Москве — это 26.09."""
    return f"{timezone.localtime(moment):%d.%m.%Y}" if moment else ""


def card_url(site_id: int) -> str:
    return reverse("admin:sites_site_card", args=[site_id])


@admin.register(Site)
class SiteAdmin(NoDeleteAdmin):
    """Каталог площадок — факты о площадке. Цены и заметки — в её карточке."""

    list_display = ("domain", "working_price", "language", "link_type", "marks_as_ad", "is_deleted")
    list_filter = (DeletedFilter, "language", "link_type", "marks_as_ad")
    search_fields = ("domain",)
    list_select_related = ("price__seller",)
    # Рабочую цену меняют кнопками карточки и действиями «Площадок», не полем
    # формы: каждая смена — заметка в истории (ADR-043).
    exclude = ("price",)
    readonly_fields = ("card_link", "working_price", "created_at", "updated_at")
    inlines = (ProductSiteInline,)

    class Media:
        js = ("seo/site-card.js",)
        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("seo/offers.css",)}

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Site]:
        # Менеджер по умолчанию прячет удалённые; здесь их скрывает
        # DeletedFilter, чтобы пометку можно было снять.
        return Site.all_objects.select_related("price__seller")

    def get_search_results(
        self, request: HttpRequest, queryset: models.QuerySet[Site], search_term: str
    ) -> tuple[models.QuerySet[Site], bool]:
        # Человек вставит адрес целиком: https://www.example.com/article.
        # Не домен (например, пустой запрос) — ищем как есть.
        with suppress(ValueError):
            search_term = normalize_domain(search_term)
        return super().get_search_results(request, queryset, search_term)

    def save_formset(
        self,
        request: HttpRequest,
        form: Any,
        formset: "BaseInlineFormSet[Any, Any, Any]",
        change: bool,
    ) -> None:
        super().save_formset(request, form, formset, change)
        if formset.model is ProductSite:
            for inline_form in formset.forms:
                if inline_form.instance.pk is not None:
                    _note_rejection(request, inline_form.instance, inline_form.changed_data)

    @admin.display(description="рабочая цена", ordering="price__placement_cents")
    def working_price(self, obj: Site) -> str:
        price = obj.price if obj.pk is not None else None
        if price is None:
            return "—"
        return f"{offers.describe(price)} · {_day(price.checked_at)}"

    @admin.display(description="карточка")
    def card_link(self, obj: Site) -> SafeString | str:
        if obj.pk is None:
            return "появится после сохранения"
        return format_html(
            '<a href="{}" data-site-card>Цены, предложения продавцов и заметки</a>',
            card_url(obj.pk),
        )

    # ---------- Карточка площадки: окно в «Площадках» или отдельная страница ----------

    def get_urls(self) -> list[URLPattern]:
        own = [
            path(
                "<int:object_id>/card/",
                self.admin_site.admin_view(require_GET(self.card_view)),
                name="sites_site_card",
            ),
            path(
                "<int:object_id>/card/fix/",
                self.admin_site.admin_view(require_POST(self.card_fix_view)),
                name="sites_site_card_fix",
            ),
            path(
                "<int:object_id>/card/keep/",
                self.admin_site.admin_view(require_POST(self.card_keep_view)),
                name="sites_site_card_keep",
            ),
            path(
                "<int:object_id>/card/note/",
                self.admin_site.admin_view(require_POST(self.card_note_view)),
                name="sites_site_card_note",
            ),
        ]
        return own + super().get_urls()

    def card_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        site = get_object_or_404(Site.all_objects.select_related("price__seller"), pk=object_id)
        if not self.has_view_permission(request, site):
            return HttpResponse(status=403)
        context = {
            **self.admin_site.each_context(request),
            **_card_context(site),
            "title": site.domain,
            "subtitle": "Карточка площадки",
            "opts": self.model._meta,
            "can_change": self.has_change_permission(request, site),
        }
        partial = request.headers.get(PARTIAL_HEADER) == "1"
        template = "admin/sites/site/card_body.html" if partial else "admin/sites/site/card.html"
        return TemplateResponse(request, template, context)

    def card_fix_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        site = get_object_or_404(Site.all_objects, pk=object_id)
        if self.has_change_permission(request, site):
            offer = get_object_or_404(SitePrice, pk=request.POST.get("offer"), site=site)
            offers.set_working_price(offer, author=request.user)
        return self._back_to_card(request, object_id)

    def card_keep_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        site = get_object_or_404(Site.all_objects, pk=object_id)
        if self.has_change_permission(request, site):
            offers.keep_current_for_sites([site.pk])
        return self._back_to_card(request, object_id)

    def card_note_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        site = get_object_or_404(Site.all_objects, pk=object_id)
        body = (request.POST.get("body") or "").strip()
        if body and self.has_change_permission(request, site):
            product = Product.objects.filter(pk=_posted_id(request, "product")).first()
            offers.add_note(site, body, product=product, author=request.user)
        return self._back_to_card(request, object_id)

    def _back_to_card(self, request: HttpRequest, object_id: int) -> HttpResponse:
        # Окно ждёт новую карточку целиком, страница без скрипта — переход на неё.
        if request.headers.get(PARTIAL_HEADER) == "1":
            return self.card_view(request, object_id)
        return HttpResponseRedirect(card_url(object_id))


def _card_context(site: Site) -> dict[str, Any]:
    """Всё для карточки: метрики, рабочая цена, предложения, история, заметки."""
    rows = list(
        ProductSiteLatest.objects.filter(site_id=site.pk)
        .select_related("product")
        .order_by("product_id")
    )
    latest = rows[0] if rows else None
    working = site.price
    working_amount = (
        Amount(working.placement_cents, working.currency, latest.placement_eur_cents)
        if working is not None and latest is not None
        else None
    )
    current = list(
        SiteOffer.objects.filter(site_id=site.pk).order_by(
            F("placement_eur_cents").asc(nulls_last=True), "pk"
        )
    )
    current_ids = {offer.pk for offer in current}
    offer_rows = []
    for offer in current:
        amount = Amount(offer.placement_cents, offer.currency, offer.placement_eur_cents)
        same_service = working is not None and offer.placement_type == working.placement_type
        offer_rows.append(
            {
                "offer": offer,
                "price": price_html(amount),
                "is_working": working is not None and offer.pk == working.pk,
                "delta": delta_html(delta(amount, working_amount))
                if same_service and working_amount is not None
                else None,
                "other_service": working is not None and not same_service,
                "pending": offer.reviewed_at is None,
                "writing": writing_html(offer.writing_cents, offer.currency),
                "announce": announce_text(offer.announce_cents, offer.currency)
                if offer.placement_type == PlacementType.GUEST_POST
                else "—",
                "gray": offers.money(offer.gray_cents, offer.currency) or "—",
            }
        )
    history = (
        SitePrice.objects.filter(site=site)
        .exclude(pk__in=current_ids)
        .select_related("seller")
        .order_by("-checked_at", "-pk")
    )
    return {
        "site": site,
        "latest": latest,
        "statuses": rows,
        "metrics_mark": seller_mark(latest.metrics_seller)
        if latest is not None and latest.metrics_trusted is False
        else "",
        "working": working,
        "working_price": price_html(working_amount) if working_amount else None,
        "working_writing": writing_html(working.writing_cents, working.currency) if working else "",
        "working_announce": announce_text(working.announce_cents, working.currency)
        if working
        else "",
        "offer_rows": offer_rows,
        "has_pending": any(row["pending"] for row in offer_rows),
        "history": [
            {
                "offer": offer,
                "price": price_html(Amount(offer.placement_cents, offer.currency, None)),
            }
            for offer in history
        ],
        "extras": [offer for offer in current if offer.extra],
        "notes": site.notes.select_related("author", "product").order_by("-created_at", "-pk"),
        "products": Product.objects.order_by("pk"),
        "fix_url": reverse("admin:sites_site_card_fix", args=[site.pk]),
        "keep_url": reverse("admin:sites_site_card_keep", args=[site.pk]),
        "note_url": reverse("admin:sites_site_card_note", args=[site.pk]),
        "add_offer_url": reverse("admin:sites_siteprice_add") + f"?site={site.pk}",
        "change_url": reverse("admin:sites_site_change", args=[site.pk]),
    }


@admin.register(ProductSite)
class ProductSiteAdmin(NoDeleteAdmin):
    list_display = ("site", "product", "status", "imported_undecided", "updated_at")
    list_filter = ("product", "status", "imported_undecided")
    search_fields = ("site__domain",)
    readonly_fields = ("site", "product", "created_at", "updated_at")
    list_select_related = ("site", "product")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: ProductSite, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        _note_rejection(request, obj, form.changed_data)


@admin.register(Seller)
class SellerAdmin(NoDeleteAdmin):
    """Продавцы (ADR-041, ADR-043). Collaborator — тоже продавец, его заводит миграция."""

    list_display = (
        "name",
        "currency",
        "sites_count",
        "working_count",
        "last_price",
        "metrics_trusted",
        "contacts",
    )
    list_editable = ("metrics_trusted",)
    search_fields = ("name",)
    fields = ("name", "currency", "contacts", "notes", "metrics_trusted", "is_collaborator")
    readonly_fields = ("is_collaborator",)

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Seller]:
        # Счётчики одним запросом на весь список, а не запросом на строку.
        working = (
            Site.objects.filter(price__seller=OuterRef("pk"))
            .order_by()
            .values("price__seller")
            .annotate(total=Count("pk"))
            .values("total")
        )
        queryset: models.QuerySet[Seller] = super().get_queryset(request)
        return queryset.annotate(
            sites_total=Count("prices__site", distinct=True),
            working_total=Subquery(working),
            last_price_at=Max("prices__checked_at"),
        )

    @admin.display(description="площадок с ценами", ordering="sites_total")
    def sites_count(self, obj: Any) -> int:
        return int(obj.sites_total)

    @admin.display(description="рабочих цен", ordering="working_total")
    def working_count(self, obj: Any) -> int:
        return int(obj.working_total or 0)

    @admin.display(description="последняя цена", ordering="last_price_at")
    def last_price(self, obj: Any) -> str:
        return _day(obj.last_price_at)


@admin.register(ExchangeRate)
class ExchangeRateAdmin(NoDeleteAdmin):
    """Курсы ЕЦБ — только просмотр: их пишет задача раз в день (ADR-043)."""

    list_display = ("currency", "rate", "rate_date", "created_at")
    list_filter = ("currency",)
    ordering = ("-rate_date", "currency")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class SiteSnapshotAdmin(SnapshotAdmin):
    """Снапшоты площадки: последний — среди замеров этой площадки."""

    snapshot_key: tuple[str, ...] = ("site_id",)
    autocomplete_fields: ClassVar[tuple[str, ...]] = ("site",)


@admin.register(SiteMetric)
class SiteMetricAdmin(SiteSnapshotAdmin):
    list_display = (
        "site",
        "dr",
        "organic_traffic",
        "total_keywords",
        "source",
        "seller",
        "checked_at",
    )
    list_filter = ("source", "seller")
    search_fields = ("site__domain",)
    list_select_related = ("site", "seller")
    autocomplete_fields = ("site", "seller")


@admin.register(SitePrice)
class SitePriceAdmin(SiteSnapshotAdmin):
    """Предложения продавцов. Новое — «Сохранить как новый объект» или «Добавить».

    Первая цена площадки становится рабочей сама; остальные ждут решения в
    карточке площадки или в «Площадках» (ADR-043).
    """

    list_display = (
        "site",
        "seller",
        "placement_type",
        "price",
        "checked_at",
        "reviewed_at",
    )
    list_filter = ("seller", "placement_type", "source")
    search_fields = ("site__domain", "seller__name")
    list_select_related = ("site", "seller")
    autocomplete_fields = ("site", "seller")
    snapshot_key = ("site_id", "seller_id", "placement_type")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return (*super().get_readonly_fields(request, obj), "reviewed_at")

    @admin.display(description="цена услуги", ordering="placement_cents")
    def price(self, obj: SitePrice) -> str:
        return offers.money(obj.placement_cents, obj.currency)

    def save_model(self, request: HttpRequest, obj: SitePrice, form: Any, change: bool) -> None:
        if not change:
            # Новое предложение ждёт решения; без рабочей цены — само станет ею.
            obj.reviewed_at = None
        super().save_model(request, obj, form, change)
        if not change and obj.site.price_id is None:
            offers.set_working_price(obj, author=request.user)


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
    """Евроценты — в евро для показа: 12000 → «€120», 12050 → «€120.50»."""
    return offers.money(cents, "EUR")


class ProductFilter(admin.SimpleListFilter):
    """Продукт, чьими глазами смотрим на площадки. Пункта «все» нет.

    Без выбора — первый активный продукт: одна площадка у двух продуктов
    дала бы две строки с разными статусами (ADR-030).
    """

    title = "продукт"
    parameter_name = "product"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        # Первый активный — из того же запроса: лишний запрос на страницу не нужен.
        products = list(Product.objects.order_by("pk").values_list("pk", "name", "is_active"))
        self.first_active = next((pk for pk, _, active in products if active), None)
        return [(str(pk), name) for pk, name, _ in products]

    def value(self) -> str | None:
        value = super().value()
        if not value and self.lookup_choices:
            first = self.first_active
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


class OffersFilter(admin.SimpleListFilter):
    """Разбор цен (ADR-043): что пришло нового, где дешевле, у кого нет цены."""

    title = "предложения"
    parameter_name = "offers"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("pending", "Есть новые, не разобраны"),
            ("cheaper", "Есть дешевле рабочей"),
            ("noprice", "Нет рабочей цены"),
        ]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        if value == "pending":
            return queryset.filter(offers_pending=True)
        if value == "cheaper":
            return queryset.filter(cheaper_id__isnull=False)
        if value == "noprice":
            return queryset.filter(price_id__isnull=True)
        return queryset


class SellerFilter(admin.SimpleListFilter):
    """Площадки, которые предлагает продавец: есть хоть одна его цена."""

    title = "продавец"
    parameter_name = "seller"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        sellers = Seller.objects.order_by("name").values_list("pk", "name")
        return [(str(pk), name) for pk, name in sellers]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        if not value:
            return queryset
        if not value.isdigit():
            return queryset.none()
        offered = SitePrice.objects.filter(site_id=OuterRef("site_id"), seller_id=int(value))
        return queryset.filter(Exists(offered))


class WritingFilter(admin.SimpleListFilter):
    """Пишет ли площадка статью сама: есть ли цена написания у рабочей цены."""

    title = "написание"
    parameter_name = "writing"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Площадка пишет сама"), ("no", "Пишем сами")]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        if self.value() == "yes":
            return queryset.filter(writing_cents__isnull=False)
        if self.value() == "no":
            return queryset.filter(price_id__isnull=False, writing_cents__isnull=True)
        return queryset


class NotesFilter(admin.SimpleListFilter):
    title = "заметки"
    parameter_name = "notes"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Есть"), ("no", "Нет")]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        if self.value() == "yes":
            return queryset.filter(notes_count__gt=0)
        if self.value() == "no":
            return queryset.filter(notes_count=0)
        return queryset


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


class SellerActionForm(helpers.ActionForm):
    """Поле «продавец» рядом с выбором действия — для «Зафиксировать продавца…»."""

    seller = forms.ModelChoiceField(
        queryset=Seller.objects.order_by("name"),
        required=False,
        label="продавец",
        empty_label="продавец…",
    )


class OffersChangeList(ChangeList):
    """Список «Площадок», который заодно достаёт предложения строк страницы.

    Одним запросом на страницу: подсказка «все цены» и «ещё N» в колонке
    «Предложения» берут их отсюда, а не запросом на строку.
    """

    def get_results(self, request: HttpRequest) -> None:
        super().get_results(request)
        by_site = offers.current_offers(row.site_id for row in self.result_list)
        for row in self.result_list:
            row.page_offers = by_site.get(row.site_id, [])


@admin.register(ProductSiteLatest)
class ProductSiteLatestAdmin(NoDeleteAdmin):
    """Площадки продукта «на сегодня» — рабочий список вместо Excel.

    Строка — это представление, поэтому сами строки не правятся. Статус
    меняется по ссылке в колонке «статус», цены и заметки — в карточке
    (окно по щелчку на домене), рабочая цена отмеченных строк — действиями.
    Всё, что в колонках, приходит одним запросом из представления, плюс
    один запрос — предложения строк страницы.
    """

    list_display = (
        "domain_link",
        "status_link",
        "dr_cell",
        "traffic_cell",
        "language",
        "price_cell",
        "offers_cell",
        "writing_cell",
        "expected_spend",
        "verdict",
        "placements_published",
        "other_products",
        "notes_cell",
    )
    list_display_links = None
    list_filter = (
        ProductFilter,
        SiteListFilter,
        WorkedFilter,
        "status",
        OffersFilter,
        SellerFilter,
        # Диапазон — поля «С» и «До» (django-admin-rangefilter, ADR-038).
        ("dr", NumericRangeFilter),
        ("organic_traffic", NumericRangeFilter),
        LanguageFilter,
        WritingFilter,
        PublishedFilter,
        NotesFilter,
    )
    search_fields = ("domain",)
    ordering = (F("dr").desc(nulls_last=True), "domain")
    list_per_page = 100
    # Полный счётчик без фильтров — лишний запрос на каждую страницу.
    show_full_result_count = False
    action_form = SellerActionForm
    actions = ("accept_new_prices_action", "fix_seller_action", "keep_current_action")

    class Media:
        js = ("seo/site-card.js",)
        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("seo/offers.css",)}

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_fix_price_permission(self, request: HttpRequest) -> bool:
        # Строки — представление, их не правят; меняется площадка: её право.
        return bool(request.user.has_perm("sites.change_site"))

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        return OffersChangeList

    def get_search_results(
        self, request: HttpRequest, queryset: models.QuerySet[Any], search_term: str
    ) -> tuple[models.QuerySet[Any], bool]:
        # Как в «Площадках»: вставленный адрес целиком сводится к домену.
        with suppress(ValueError):
            search_term = normalize_domain(search_term)
        return super().get_search_results(request, queryset, search_term)

    # ---------- Колонки ----------

    @admin.display(description="домен", ordering="domain")
    def domain_link(self, obj: ProductSiteLatest) -> SafeString:
        # Карточка открывается окном без перезагрузки (seo/site-card.js),
        # без скрипта — отдельной страницей.
        return format_html(
            '<a href="{}" data-site-card title="Карточка площадки">{}</a>',
            card_url(obj.site_id),
            obj.domain,
        )

    @admin.display(description="статус", ordering="status")
    def status_link(self, obj: ProductSiteLatest) -> SafeString:
        url = reverse("admin:sites_productsite_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.get_status_display())

    @admin.display(description="DR", ordering="dr")
    def dr_cell(self, obj: ProductSiteLatest) -> SafeString:
        mark = seller_mark(obj.metrics_seller) if obj.metrics_trusted is False else ""
        return format_html("{}{}", "" if obj.dr is None else obj.dr, mark)

    @admin.display(description="трафик", ordering="organic_traffic")
    def traffic_cell(self, obj: ProductSiteLatest) -> str:
        return "" if obj.organic_traffic is None else f"{obj.organic_traffic}"

    @admin.display(description="цена", ordering="placement_eur_cents")
    def price_cell(self, obj: ProductSiteLatest) -> SafeString:
        if obj.price_id is None:
            return format_html('<span class="seo-flat">{}</span>', "нет цены")
        amount = Amount(obj.placement_cents, obj.price_currency or "EUR", obj.placement_eur_cents)
        service = PlacementType(obj.price_type).label if obj.price_type else ""
        return format_html(
            '{}<div class="seo-sub">{} · {}</div>', price_html(amount), service, obj.price_seller
        )

    @admin.display(description="предложения")
    def offers_cell(self, obj: ProductSiteLatest) -> SafeString:
        working = Amount(obj.placement_cents, obj.price_currency or "EUR", obj.placement_eur_cents)
        chips = []
        if obj.new_price_id is not None:
            new = Amount(obj.new_price_cents, obj.new_price_currency or "EUR", None)
            change = delta(new, working)
            arrow = "▲" if change is not None and change.up else "▼"
            kind = ""
            if obj.new_price_pending:
                kind = "seo-up" if change is not None and change.up else "seo-down"
            chips.append(
                format_html(
                    '<span class="seo-chip {}">{} новая {}</span>',
                    kind,
                    arrow,
                    offers.money(obj.new_price_cents, obj.new_price_currency or "EUR"),
                )
            )
        if obj.cheaper_id is not None and obj.cheaper_eur_cents is not None:
            cheaper = Amount(
                obj.cheaper_cents, obj.cheaper_currency or "EUR", obj.cheaper_eur_cents
            )
            change = delta(cheaper, working)
            if change is not None:
                approx = "≈" if change.approx else ""
                chips.append(
                    format_html(
                        '<span class="seo-chip {}">▼ {}{} · {}</span>',
                        "seo-down" if obj.cheaper_pending else "",
                        approx,
                        offers.money(abs(change.cents), change.currency),
                        obj.cheaper_seller,
                    )
                )
        page_offers: list[SiteOffer] = getattr(obj, "page_offers", [])
        others = len([offer for offer in page_offers if offer.pk != obj.price_id])
        if not chips and others:
            chips.append(format_html('<span class="seo-flat">ещё {}</span>', others))
        if not chips:
            return format_html('<span class="seo-flat">{}</span>', "—")
        tip = _offers_tip(page_offers, obj)
        return format_html(
            '<span class="seo-offers" title="{}">{}</span>',
            tip,
            format_html(" ".join(["{}"] * len(chips)), *chips),
        )

    @admin.display(description="написание", ordering="writing_eur_cents")
    def writing_cell(self, obj: ProductSiteLatest) -> SafeString:
        return writing_html(obj.writing_cents, obj.price_currency or "EUR")

    @admin.display(description="плановые расходы", ordering="expected_spend_cents")
    def expected_spend(self, obj: ProductSiteLatest) -> str:
        if obj.expected_spend_cents is None:
            return ""
        euros = obj.expected_spend_cents
        return _euros(euros if obj.price_currency == "EUR" else round_euros(euros))

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

    @admin.display(description="заметки", ordering="notes_count")
    def notes_cell(self, obj: ProductSiteLatest) -> SafeString | str:
        if not obj.notes_count:
            return ""
        last = obj.last_note or ""
        when = _day(obj.last_note_at)
        return format_html(
            '<a class="seo-notes" href="{}" data-site-card title="{}">💬 {}</a>',
            card_url(obj.site_id),
            f"{last}\n{when}".strip(),
            obj.notes_count,
        )

    # ---------- Действия над рабочей ценой отмеченных строк ----------

    @admin.action(description="Принять новые цены", permissions=["fix_price"])
    def accept_new_prices_action(
        self, request: HttpRequest, queryset: models.QuerySet[ProductSiteLatest]
    ) -> None:
        result = offers.accept_new_prices(_site_ids(queryset), author=request.user)
        self._report(request, result, "новой цены того же продавца нет")

    @admin.action(description="Зафиксировать продавца…", permissions=["fix_price"])
    def fix_seller_action(
        self, request: HttpRequest, queryset: models.QuerySet[ProductSiteLatest]
    ) -> None:
        seller = Seller.objects.filter(pk=_posted_id(request, "seller")).first()
        if seller is None:
            self.message_user(request, "Выберите продавца рядом с действием.", messages.WARNING)
            return
        result = offers.fix_seller(_site_ids(queryset), seller, author=request.user)
        self._report(request, result, f"нет предложения {seller} или оно уже рабочее")

    @admin.action(description="Оставить как есть", permissions=["fix_price"])
    def keep_current_action(
        self, request: HttpRequest, queryset: models.QuerySet[ProductSiteLatest]
    ) -> None:
        sites = offers.keep_current_for_sites(_site_ids(queryset))
        text = f"Разобрано: {sites}. Рабочие цены не менялись."
        self.message_user(request, text, messages.SUCCESS)

    def _report(self, request: HttpRequest, result: offers.Outcome, skip_reason: str) -> None:
        text = f"Рабочая цена изменена: {result.changed}."
        if result.skipped:
            text += f" Пропущено: {result.skipped} — {skip_reason}."
        self.message_user(request, text, messages.SUCCESS if result.changed else messages.WARNING)


def _posted_id(request: HttpRequest, name: str) -> int:
    """Номер из формы; не номер — 0: такой записи нет, фильтр ничего не найдёт."""
    value = request.POST.get(name) or ""
    return int(value) if value.isdigit() else 0


def _site_ids(queryset: models.QuerySet[ProductSiteLatest]) -> list[int]:
    return list(queryset.order_by().values_list("site_id", flat=True).distinct())


def _offers_tip(page_offers: list[SiteOffer], obj: ProductSiteLatest) -> str:
    """Подсказка у «Предложений»: все текущие цены площадки, рабочая отмечена."""
    working = Amount(obj.placement_cents, obj.price_currency or "EUR", obj.placement_eur_cents)
    lines = []
    for offer in page_offers:
        amount = Amount(offer.placement_cents, offer.currency, offer.placement_eur_cents)
        service = PlacementType(offer.placement_type).label
        price = offers.money(offer.placement_cents, offer.currency)
        line = f"{offer.seller_name} · {service} {price}"
        if not amount.is_euro and amount.eur_cents is not None:
            line += f" ≈ {offers.money(round_euros(amount.eur_cents), 'EUR')}"
        if offer.writing_cents is not None:
            line += f" · 📝 {offers.money(offer.writing_cents, offer.currency)}"
        line += f" · {_day(offer.checked_at)}"
        if offer.pk == obj.price_id:
            line += " — рабочая"
        elif offer.placement_type != obj.price_type:
            line += " (другая услуга)"
        else:
            change = delta(amount, working)
            if change is not None:
                line += f"  {delta_text(change)}"
        lines.append(line)
    return "\n".join(lines)


# Экран «Загрузки» (E1-08) — в своём модуле; регистрируется при загрузке этого.
from apps.sites import upload_admin  # noqa: E402, F401
