"""Админка блока 1: продукты, площадки, решения, продавцы, цены, списки.

Удаление отключено везде: ничего не удаляем физически. Площадку
скрывает пометка «удалена», решение по ней меняется статусом. Исключения —
локальные настройки на странице продукта (ADR-035) и продукт, с которым
ещё не работали (ADR-036).

Рабочая цена, предложения продавцов, заметки и карточка площадки —
ADR-043; правила смены цены — `apps/sites/offers.py`. Серость в Google в
карточке — E2-06, `apps/sites/gray_scan.py`.
"""

import datetime as dt
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from contextlib import suppress
from typing import Any, ClassVar

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.views.main import ChangeList
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Case, Count, Exists, F, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from rangefilter.filters import NumericRangeFilter

from apps.content.admin import (
    PRODUCT_SETTING_FIELDS,
    ProductOtherSettingsInline,
    ProductSettingsForm,
)
from apps.content.domain_settings import gray_terms, gray_zones
from apps.content.models import DomainSetting
from apps.placements import invoices
from apps.placements.models import Invoice, InvoiceStatus, Placement
from apps.sites import ahrefs_domains, countries, gray_scan, offers
from apps.sites import export as site_export
from apps.sites.display import (
    Amount,
    announce_text,
    delta,
    delta_html,
    delta_text,
    domain_tools_html,
    price_html,
    round_euros,
    seller_mark,
    site_url,
    writing_html,
)
from apps.sites.domains import normalize_domain
from apps.sites.export import REGION_AT, REGION_KEYWORDS, REGION_TRAFFIC
from apps.sites.models import (
    ExchangeRate,
    GrayScan,
    PlacementType,
    Product,
    ProductRefDomain,
    ProductSite,
    ProductSiteLatest,
    Seller,
    Site,
    SiteAudit,
    SiteCountryLatest,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteOffer,
    SitePrice,
    SiteStatus,
)
from apps.sites.site_card import STATUS_TONES, euros, fields_text, note_rows, notes_summary
from apps.sites.site_card import price_summary as card_price_summary
from apps.sites.status_history import site_history
from apps.workspace import card as card_sections
from apps.workspace.products import WorkingProductFilter, products_of, working_product_id
from config import export
from config.admin import ModelAdmin, NoDeleteAdmin, SnapshotAdmin, TabularInline, is_partial
from config.assets import Css, Js
from config.export import attachment
from config.forms import ChoiceButtons


@admin.register(Product)
class ProductAdmin(NoDeleteAdmin):
    """Продукт и его настройки: заводя продукт, человек сразу видит, что заполнить."""

    panel = True
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

    panel = True
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
        css: ClassVar[dict[str, tuple[Css, ...]]] = {"all": (Css("seo/offers.css"),)}

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
        # Открывается в той же панели (seo/panel.js), на полной странице — тоже панелью.
        return format_html(
            '<a href="{}" data-panel>Цены, предложения продавцов и заметки</a>',
            card_url(obj.pk),
        )

    # ---------- Карточка площадки: панель справа или отдельная страница ----------

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
            path(
                "<int:object_id>/card/gray/",
                self.admin_site.admin_view(require_POST(self.card_gray_view)),
                name="sites_site_card_gray",
            ),
        ]
        return own + super().get_urls()

    def card_view(
        self, request: HttpRequest, object_id: int, gray_form: "GrayReadingForm | None" = None
    ) -> HttpResponse:
        """Карточка: панели (заголовок X-Seo-Partial) — только содержимое, иначе страница.

        `gray_form` — форма замера серости с ошибками: карточка покажет их у полей.
        """
        site = get_object_or_404(Site.all_objects.select_related("price__seller"), pk=object_id)
        if not self.has_view_permission(request, site):
            return HttpResponse(status=403)
        partial = is_partial(request)
        gray = _gray_context(site, gray_form)
        context = {
            **self.admin_site.each_context(request),
            **_card_context(site, working_product_id(request)),
            "gray": gray,
            # Свёрнутые разделы — у пользователя, на все карточки (ADR-058):
            # сервер сразу отдаёт их свёрнутыми, без мигания при открытии.
            "closed": card_sections.closed_sections(request.user),
            "title": site.domain,
            "subtitle": "Карточка площадки",
            "opts": self.model._meta,
            "can_change": self.has_change_permission(request, site),
            "panel": partial,
            "panel_title": site.domain,
            # Название — ссылка на сайт в новой вкладке, рядом — значки «открыть
            # сайт» и «скопировать домен».
            "panel_title_url": site_url(site.domain),
            "panel_title_tools": domain_tools_html(site.domain),
            "panel_sub": "Карточка площадки",
            "panel_links": [
                ("Факты о площадке", reverse("admin:sites_site_change", args=[site.pk]), True)
            ],
            "panel_full_url": card_url(site.pk),
        }
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

    def card_gray_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        """Замер серости из карточки: числа «About N results» двух запросов (E2-06)."""
        site = get_object_or_404(Site.all_objects, pk=object_id)
        if not self.has_change_permission(request, site):
            return self._back_to_card(request, object_id)
        form = GrayReadingForm(request.POST)
        if not form.is_valid():
            if is_partial(request):
                return self.card_view(request, object_id, gray_form=form)
            return self._back_to_card(request, object_id)
        gray_scan.record(site, form.reading())
        return self._back_to_card(request, object_id)

    def _back_to_card(self, request: HttpRequest, object_id: int) -> HttpResponse:
        # Панель ждёт новую карточку целиком, страница без скрипта — переход на неё.
        if is_partial(request):
            return self.card_view(request, object_id)
        return HttpResponseRedirect(card_url(object_id))


def _card_context(site: Site, working_product: int | None = None) -> dict[str, Any]:
    """Всё для карточки: метрики, статусы с историей, рабочая цена, предложения,
    история цен, заметки. Рабочий продукт пользователя — первым (E9-13)."""
    rows = sorted(
        ProductSiteLatest.objects.filter(site_id=site.pk).select_related("product"),
        key=lambda row: (row.product_id != working_product, row.product_id),
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
    status_history = site_history(site.pk)
    # Ссылается ли площадка на продукт — по его выгрузке Ahrefs (ADR-051).
    refs = {ref.product_id: ref for ref in ProductRefDomain.objects.filter(domain=site.domain)}
    notes = note_rows(site.notes.select_related("author", "product").order_by("-created_at", "-pk"))
    extras = [offer for offer in current if offer.extra]
    pending = sum(1 for row in offer_rows if row["pending"] and not row["is_working"])
    working_eur = euros(working_amount.eur_cents) if working_amount else ""
    if working is not None and not working_eur:
        # Курса нет — в евро не пересчитать, показываем как есть.
        working_eur = offers.money(working.placement_cents, working.currency)
    return {
        "site": site,
        "latest": latest,
        "statuses": [
            {
                "row": row,
                "tone": STATUS_TONES.get(SiteStatus(row.status), "info"),
                "change_url": reverse("admin:sites_productsite_change", args=[row.pk]),
                "decision_url": reverse("admin:sites_productsite_decision", args=[row.pk]),
                # Строка «Площадок» — та же строка product_sites (pk общий).
                "history": status_history.get(row.pk, []),
                "ref": refs.get(row.product_id),
            }
            for row in rows
        ],
        "geo_flag": countries.flag_html(latest.top_geo) if latest is not None else "",
        "traffic_text": gray_scan.count_text(latest.organic_traffic if latest else None),
        "geo_traffic_text": gray_scan.count_text(latest.top_geo_traffic)
        if latest is not None and latest.top_geo_traffic is not None
        else "",
        # Плитка «Рабочая цена»: крупно в евро, мелко — исходная цена и услуга.
        "working_eur": working_eur,
        "working_original": offers.money(working.placement_cents, working.currency)
        if working is not None and working.currency != "EUR"
        else "",
        "price_summary": card_price_summary(
            working_eur, working.seller.name if working else "", len(offer_rows), pending
        ),
        "has_gray_price": any(offer.gray_cents is not None for offer in current),
        "pending_count": pending,
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
        "extras": extras,
        "extras_summary": " · ".join(
            f"{offer.seller_name} · {fields_text(len(offer.extra or {}))}" for offer in extras
        )
        or "нет",
        "notes": notes,
        "notes_summary": notes_summary(notes),
        "products": Product.objects.order_by("pk"),
        # Новая заметка — к рабочему продукту пользователя.
        "note_product": working_product,
        "fix_url": reverse("admin:sites_site_card_fix", args=[site.pk]),
        "keep_url": reverse("admin:sites_site_card_keep", args=[site.pk]),
        "note_url": reverse("admin:sites_site_card_note", args=[site.pk]),
        "add_offer_url": reverse("admin:sites_siteprice_add") + f"?site={site.pk}",
        "change_url": reverse("admin:sites_site_change", args=[site.pk]),
    }


class GrayReadingForm(forms.Form):
    """Замер серости из карточки: два числа из Google и запросы, по которым искали."""

    total = forms.IntegerField(
        label="Всего",
        min_value=0,
        error_messages={"required": "Впишите, сколько всего страниц в индексе."},
    )
    gray = forms.IntegerField(
        label="Серых",
        min_value=0,
        error_messages={"required": "Впишите, сколько серых страниц."},
    )
    total_query = forms.CharField(max_length=4000)
    gray_query = forms.CharField(max_length=4000, required=False)
    sample_urls = forms.CharField(required=False)
    source = forms.ChoiceField(
        choices=[(gray_scan.SOURCE_TYPED, "вручную"), (gray_scan.SOURCE_EXTENSION, "расширение")],
        required=False,
    )

    def clean_sample_urls(self) -> list[str]:
        # Примеры — по адресу на строку; не адрес страницы — не пример.
        urls: list[str] = []
        for line in self.cleaned_data["sample_urls"].splitlines():
            url = line.strip()
            if url.startswith(("http://", "https://")) and len(url) <= 2000 and url not in urls:
                urls.append(url)
        return urls[: gray_scan.SAMPLE_LIMIT]

    def reading(self) -> gray_scan.Reading:
        data = self.cleaned_data
        return gray_scan.Reading(
            total=data["total"],
            gray=data["gray"],
            total_query=data["total_query"],
            gray_query=data["gray_query"],
            sample_urls=data["sample_urls"],
            source=data["source"] or gray_scan.SOURCE_TYPED,
        )


def _with_gray_zone(queryset: models.QuerySet[Any]) -> models.QuerySet[Any]:
    """Зона последнего замера серости — `gray_zone`, по общей настройке GRAY_ZONES.

    Границы — подзапросом в том же запросе списка, а не отдельным чтением
    настройки: «Площадки» держат число запросов на страницу. Настройки нет —
    сравнения дают NULL, и зоны нет. Правило — как `gray_scan.zone_of`.
    """
    setting = DomainSetting.objects.filter(key="GRAY_ZONES", product__isnull=True)

    def bound(name: str) -> Subquery:
        value = Cast(KT(f"value__{name}"), models.DecimalField(max_digits=6, decimal_places=2))
        return Subquery(setting.annotate(bound=value).values("bound")[:1])

    green, yellow = bound("green"), bound("yellow")
    annotated: models.QuerySet[Any] = queryset.annotate(
        gray_zone=Case(
            When(gray_ratio__lt=green, then=Value("green")),
            When(gray_ratio__lte=yellow, then=Value("yellow")),
            When(gray_ratio__gt=yellow, then=Value("red")),
            default=None,
            output_field=models.TextField(),
        )
    )
    return annotated


# Замеров серости в истории карточки.
GRAY_HISTORY = 20
GRAY_SOURCES = {gray_scan.SOURCE_EXTENSION: "расширение", gray_scan.SOURCE_TYPED: "вручную"}


def _gray_context(site: Site, form: GrayReadingForm | None) -> dict[str, Any]:
    """Раздел «Серость в Google»: кнопки, форма замера, последний замер и история."""
    try:
        terms = gray_terms()
    except ImproperlyConfigured:
        # Общее значение стёрли руками: кнопка «всего» работает и без него.
        terms = ()
    zones = gray_zones()
    total_query = gray_scan.total_query(site.domain)
    gray_query = gray_scan.gray_query(site.domain, terms) if terms else ""
    rows = []
    for scan in site.gray_scans.order_by("-checked_at", "-pk")[:GRAY_HISTORY]:
        zone = gray_scan.zone_of(scan.ratio, zones)
        source = str((scan.breakdown or {}).get("source") or "")
        rows.append(
            {
                "when": f"{timezone.localtime(scan.checked_at):%d.%m.%Y %H:%M}",
                "day": _day(scan.checked_at),
                "percent": gray_scan.percent_text(scan.ratio),
                "zone": zone,
                "zone_title": gray_scan.ZONE_TITLES[zone] if zone else "",
                "total": gray_scan.count_text(scan.total_indexed),
                "gray": gray_scan.count_text(scan.gray_hits),
                "over_total": bool((scan.breakdown or {}).get("over_total")),
                "source": GRAY_SOURCES.get(source, source or "—"),
                "samples": scan.sample_urls or [],
            }
        )
    setting = DomainSetting.objects.filter(key="GRAY_TERMS", product__isnull=True).first()
    words = gray_scan.word_count(gray_query)
    return {
        "total_query": total_query,
        "gray_query": gray_query,
        "total_url": gray_scan.google_url(total_query),
        "gray_url": gray_scan.google_url(gray_query) if gray_query else "",
        "words": words,
        "word_limit": gray_scan.GOOGLE_WORD_LIMIT,
        "too_long": words > gray_scan.GOOGLE_WORD_LIMIT,
        "zones": zones,
        "terms_count": len(terms),
        "settings_url": reverse("admin:content_domainsetting_change", args=[setting.pk])
        if setting is not None
        else "",
        "save_url": reverse("admin:sites_site_card_gray", args=[site.pk]),
        "form": form or GrayReadingForm(),
        "latest": rows[0] if rows else None,
        "history": rows,
    }


# Ряды кнопок статуса площадки: путь площадки, отказы, аудит (ADR-047).
SITE_STATUS_ROWS = (SiteStatus.DISCARDED, SiteStatus.AUDITING)


class DecisionForm(forms.ModelForm):  # type: ignore[type-arg]
    """Решение по площадке: статус кнопками по порядку и причина отказа.

    Панель «Решение по площадке» в «Площадках»; у полной формы решения те же
    кнопки (`ProductSiteAdmin.formfield_for_dbfield`).
    """

    class Meta:
        model = ProductSite
        fields = ("status", "reject_reason")
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "status": ChoiceButtons(rows=SITE_STATUS_ROWS),
            "reject_reason": forms.Textarea(attrs={"rows": 3}),
        }


@admin.register(ProductSite)
class ProductSiteAdmin(NoDeleteAdmin):
    """Решения по площадкам: статус площадки у продукта (ADR-030).

    В «Площадках» статус открывает решение панелью справа (`decision_view`,
    seo/panel.js, E9-11); полная форма — здесь, с теми же кнопками статуса.
    """

    list_display = ("site", "product", "status", "imported_undecided", "updated_at")
    list_filter = (WorkingProductFilter, "status", "imported_undecided")
    search_fields = ("site__domain",)
    readonly_fields = ("site", "product", "created_at", "updated_at")
    list_select_related = ("site", "product")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def formfield_for_dbfield(
        self, db_field: "models.Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> forms.Field | None:
        if db_field.name == "status":
            kwargs["widget"] = ChoiceButtons(rows=SITE_STATUS_ROWS)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request: HttpRequest, obj: ProductSite, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        _note_rejection(request, obj, form.changed_data)

    def get_urls(self) -> list[URLPattern]:
        own = [
            path(
                "<int:object_id>/decision/",
                self.admin_site.admin_view(self.decision_view),
                name="sites_productsite_decision",
            ),
        ]
        return own + super().get_urls()

    def decision_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        """Решение панелью: GET — форма, POST — сохранить.

        Панель (заголовок X-Seo-Partial) получает только форму, после записи —
        JSON с подписью для сообщения; строку списка панель обновит сама.
        Без скрипта — полная форма решения.
        """
        row = get_object_or_404(ProductSite.objects.select_related("site", "product"), pk=object_id)
        change_url = reverse("admin:sites_productsite_change", args=[row.pk])
        if not is_partial(request):
            return HttpResponseRedirect(change_url)
        if not self.has_change_permission(request, row):
            return HttpResponse(status=403)
        form = DecisionForm(request.POST or None, instance=row)
        if request.method == "POST" and form.is_valid():
            form.save()
            _note_rejection(request, row, form.changed_data)
            label = f"{row.site.domain} · {row.product.name} — {row.get_status_display()}"
            return JsonResponse({"saved": True, "message": label})
        context = {
            "form": form,
            "row": row,
            "action_url": reverse("admin:sites_productsite_decision", args=[row.pk]),
            "panel_title": f"{row.site.domain} · {row.product.name}",
            "panel_sub": "Решение по площадке",
            "panel_links": [("Карточка площадки", card_url(row.site_id), True)],
            "panel_full_url": change_url,
            "panel_can_save": True,
        }
        # Ошибка в форме — та же форма с подсказками, код 200, как у формы записи.
        return TemplateResponse(request, "admin/sites/productsite/decision_body.html", context)


@admin.register(Seller)
class SellerAdmin(NoDeleteAdmin):
    """Продавцы (ADR-041, ADR-043). Collaborator — тоже продавец, его заводит миграция.

    Счета продавца (E1-14, ADR-055): в списке — к оплате и заплачено, в карточке —
    неоплаченные счета и ссылки на все счета и на новый.
    """

    panel = True
    list_display = (
        "name",
        "currency",
        "sites_count",
        "working_count",
        "last_price",
        "due_cell",
        "paid_cell",
        "metrics_trusted",
        "contacts",
    )
    list_editable = ("metrics_trusted",)
    search_fields = ("name",)
    fields = ("name", "currency", "contacts", "notes", "metrics_trusted", "is_collaborator")
    readonly_fields = ("is_collaborator", "invoices_block")

    class Media:
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            "all": (Css("seo/offers.css"), Css("seo/status-history.css"), Css("seo/invoices.css"))
        }

    def get_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        fields = tuple(super().get_fields(request, obj))
        fields = tuple(field for field in fields if field != "invoices_block")
        return (*fields, "invoices_block") if obj is not None else fields

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
        # Счета — одним запросом на страницу, суммы по валютам считает Python.
        live = Invoice.objects.exclude(status=InvoiceStatus.CANCELLED).only(
            "seller_id", "status", "amount_cents", "currency"
        )
        return queryset.annotate(
            sites_total=Count("prices__site", distinct=True),
            working_total=Subquery(working),
            last_price_at=Max("prices__checked_at"),
        ).prefetch_related(models.Prefetch("invoices", queryset=live, to_attr="live_invoices"))

    @admin.display(description="к оплате")
    def due_cell(self, obj: Any) -> str:
        summary = invoices.seller_money(obj.live_invoices)
        if not summary.due_count:
            return ""
        return f"{invoices.sums_text(summary.due)} · счетов: {summary.due_count}"

    @admin.display(description="заплачено по счетам")
    def paid_cell(self, obj: Any) -> str:
        return invoices.sums_text(invoices.seller_money(obj.live_invoices).paid)

    @admin.display(description="счета")
    def invoices_block(self, obj: Any) -> SafeString | str:
        """Неоплаченные счета, сколько заплачено; ссылки на все счета и на новый (E1-14)."""
        if obj is None or obj.pk is None:
            return "—"
        rows = list(
            Invoice.objects.filter(seller=obj)
            .exclude(status=InvoiceStatus.CANCELLED)
            .order_by("issued_on", "pk")
            .prefetch_related("items__placement__site")
        )
        summary = invoices.seller_money(rows)
        due = [
            (
                reverse("admin:placements_invoice_change", args=[invoice.pk]),
                f"{invoice.issued_on:%d.%m.%Y}",
                offers.money(invoice.amount_cents, invoice.currency),
                ", ".join(item.placement.site.domain for item in invoice.items.all()) or "—",
            )
            for invoice in rows
            if invoice.status == InvoiceStatus.ISSUED
        ]
        all_url = reverse("admin:placements_invoice_changelist") + f"?seller__id__exact={obj.pk}"
        new_url = reverse("admin:placements_invoice_add") + f"?seller={obj.pk}"
        paid = invoices.sums_text(summary.paid) or "—"
        return format_html(
            '<div class="seo-seller-invoices">'
            "<div>К оплате: <b>{}</b></div>{}"
            "<div>Заплачено по счетам: <b>{}</b>{}</div>"
            '<div class="seo-seller-invoice-links"><a href="{}">Все счета продавца</a>'
            ' · <a href="{}" data-panel>Новый счёт</a></div></div>',
            invoices.sums_text(summary.due) or "ничего",
            format_html(
                '<ul class="seo-status-list">{}</ul>',
                format_html_join(
                    "",
                    '<li><a href="{}" data-panel>{} · {}</a> <span class="seo-sub">{}</span></li>',
                    due,
                ),
            )
            if due
            else "",
            paid,
            format_html(' <span class="seo-sub">счетов: {}</span>', summary.paid_count)
            if summary.paid_count
            else "",
            all_url,
            new_url,
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
    list_filter = (WorkingProductFilter, "verdict", "author")
    search_fields = ("site__domain",)
    list_select_related = ("site", "product")
    snapshot_key = ("site_id", "product_id")
    time_field = "created_at"


@admin.register(SiteList)
class SiteListAdmin(NoDeleteAdmin):
    """Рабочие списки (ADR-033). Создаёт их импорт; здесь — обзор и имя."""

    panel = True
    list_display = (
        "name",
        "sites_count",
        "first_seen_count",
        "ahrefs_link",
        "source",
        "created_at",
    )
    search_fields = ("name",)
    readonly_fields = ("created_at", "sites_count", "first_seen_count")

    def get_urls(self) -> list[URLPattern]:
        view = self.admin_site.admin_view
        own = [
            path(
                "<int:list_id>/ahrefs/",
                view(require_GET(self.ahrefs_view)),
                name="sites_sitelist_ahrefs",
            ),
        ]
        return own + super().get_urls()

    def ahrefs_view(self, request: HttpRequest, list_id: int) -> HttpResponse:
        """«Домены для Ahrefs»: части по 500, файл части (?part=N) или все архивом (?zip=1)."""
        site_list = get_object_or_404(SiteList, pk=list_id)
        all_parts = ahrefs_domains.parts(site_list)
        part = request.GET.get("part")
        if part:
            chosen = next((p for p in all_parts if str(p.number) == part), None)
            if chosen is None:
                return HttpResponseRedirect(request.path)
            return attachment(chosen.text().encode(), chosen.file_name(site_list), "text/plain")
        if request.GET.get("zip") and all_parts:
            name, content = ahrefs_domains.archive(site_list, all_parts)
            return attachment(content, name, "application/zip")
        context = {
            **self.admin_site.each_context(request),
            "title": f"Домены для Ahrefs: {site_list.name}",
            "opts": self.model._meta,
            "site_list": site_list,
            "parts": all_parts,
            "total": sum(len(p.domains) for p in all_parts),
            "part_size": ahrefs_domains.PART_SIZE,
            "sites_url": reverse("admin:sites_productsitelatest_changelist")
            + f"?list={site_list.pk}",
            "upload_url": reverse("admin:sites_upload_add"),
        }
        return TemplateResponse(request, "admin/sites/sitelist/ahrefs.html", context)

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

    @admin.display(description="Ahrefs")
    def ahrefs_link(self, obj: Any) -> SafeString:
        url = reverse("admin:sites_sitelist_ahrefs", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, "Домены для Ahrefs")


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
    """Продукт, чьими глазами смотрим на площадки. Пункта «все» нет: одна
    площадка у двух продуктов дала бы две строки с разными статусами (ADR-030).

    Без выбора — рабочий продукт пользователя (ADR-057).
    """

    title = "продукт"
    parameter_name = "product"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        self.working = working_product_id(request)
        return [(str(pk), name) for pk, name, _ in products_of(request)]

    def value(self) -> str | None:
        value = super().value()
        if not value and self.lookup_choices:
            known = any(pk == str(self.working) for pk, _ in self.lookup_choices)
            value = str(self.working) if known else self.lookup_choices[0][0]
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


class RefsFilter(admin.SimpleListFilter):
    """«Ссылаются на нас» — по выгрузке Ahrefs «Referring domains» продукта (ADR-051).

    Без выбора площадки, которые уже ссылаются на продукт, скрыты: второй
    ссылкой с того же домена не выиграть. Ссылка пропала — дата Lost у Ahrefs
    или домена нет в новой выгрузке продукта: такие видны, их можно выбрать.
    Домен — точное совпадение. У продукта без выгрузки скрывать нечего.
    """

    title = "ссылаются на нас"
    parameter_name = "refs"
    HIDE, ONLY, LOST, ALL = "hide", "only", "lost", "all"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            (self.HIDE, "Скрыть"),
            (self.ONLY, "Только они"),
            (self.LOST, "Ссылка пропала"),
            (self.ALL, "Показать все"),
        ]

    def value(self) -> str | None:
        value = super().value()
        if not value:
            value = self.HIDE
            # Запоминаем, чтобы пункт по умолчанию был выбран и в колонке фильтров.
            self.used_parameters[self.parameter_name] = value
        return str(value)

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        value = self.value()
        if value == self.ALL:
            return queryset
        if value == self.ONLY:
            return queryset.filter(pk__in=_ref_rows(linking=True))
        if value == self.LOST:
            return queryset.filter(pk__in=_ref_rows(linking=False))
        return queryset.exclude(pk__in=_ref_rows(linking=True))

    def choices(self, changelist: Any) -> Iterator[Any]:
        # Первый пункт Django — «Все»; у нас без выбора — «Скрыть», «Показать все» — свой.
        choices = super().choices(changelist)
        next(choices)
        yield from choices


def _ref_rows(*, linking: bool) -> Subquery:
    """Строки «Площадок» (id `product_sites`), чей домен в списке продукта (ADR-051).

    Подзапрос по таблицам, а не по представлению «Площадок»: `Exists` по
    представлению сразу после записи выгрузки, пока у таблицы нет статистики,
    Postgres перебирал все домены на каждую площадку — «Площадки» 6 с вместо
    0,5 с (замер E1-09). Подзапрос считается один раз.
    """
    refs = ProductRefDomain.objects.filter(
        product_id=OuterRef("product_id"), domain=OuterRef("site__domain")
    )
    linking_now = Q(lost_at__isnull=True, missing_since__isnull=True)
    refs = refs.filter(linking_now) if linking else refs.exclude(linking_now)
    return Subquery(ProductSite.objects.filter(Exists(refs)).values("pk"))


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


# Колонки региона (ADR-045): их нет в представлении, их добавляет get_queryset.
REGION_PARAMS = [
    f"{field}__range__{edge}"
    for field in (REGION_TRAFFIC, REGION_KEYWORDS)
    for edge in ("gte", "lte")
]


def _region(request: HttpRequest) -> str | None:
    """Выбранный регион — код страны строчными, или None — «Все»."""
    value = (request.GET.get(RegionFilter.parameter_name) or "").strip().lower()
    return value if len(value) == 2 and value.isalpha() else None


class RegionFilter(admin.SimpleListFilter):
    """Регион (ADR-045): колонки «трафик» и «ключи» страны и фильтры «от/до» по ним.

    Строки не отбирает. В списке — только страны, под которые загружали
    выгрузку Ahrefs по стране, с числом площадок: по топ-региону выгрузки
    «все страны» трафик других стран не узнать. «Все» — колонок региона нет.
    """

    title = "регион"
    parameter_name = "region"
    # Страны с флагами и поиском поверх выпадающего списка (seo/country-picker.js).
    template = "admin/sites/region_filter.html"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        rows = (
            SiteCountryMetric.objects.values("country")
            .annotate(sites=Count("site_id", distinct=True))
            .order_by("-sites", "country")
        )
        return [
            (
                row["country"],
                f"{countries.name(row['country'])} · {row['country'].upper()} ({row['sites']})",
            )
            for row in rows
        ]

    def queryset(self, request: HttpRequest, queryset: models.QuerySet[Any]) -> Any:
        return queryset

    def choices(self, changelist: Any) -> Iterator[Any]:
        # Другой регион — другие колонки: его «от/до» и сортировка сбрасываются.
        reset = ["o", *REGION_PARAMS]
        yield {
            "selected": self.value() is None,
            "query_string": changelist.get_query_string(remove=[self.parameter_name, *reset]),
            "display": "Все",
            "flag": countries.GLOBE,
            "search": "все all",
        }
        for lookup, title in self.lookup_choices:
            country = countries.get(lookup)
            yield {
                "selected": self.value() == lookup,
                "query_string": changelist.get_query_string(
                    {self.parameter_name: lookup}, remove=reset
                ),
                "display": title,
                "flag": countries.flag_code(lookup),
                "search": country.search if country else lookup,
            }


def _region_range(field_path: str, title: str) -> type:
    """«от — до» по колонке региона. Поля такого в модели нет — даём фильтру поле-заглушку."""

    class RegionRangeFilter(NumericRangeFilter):  # type: ignore[misc]
        def __init__(self, request: HttpRequest, params: Any, model: Any, model_admin: Any) -> None:
            field = models.IntegerField(verbose_name=title)
            super().__init__(field, request, params, model, model_admin, field_path)

    return RegionRangeFilter


def _region_columns(code: str) -> list[Any]:
    """Колонки «трафик US» и «ключи US» — подписи свои на каждый запрос."""
    label = code.upper()

    def cell(value: int | None, at: dt.datetime | None) -> SafeString | str:
        if value is None:
            return ""
        when = f"{timezone.localtime(at):%d.%m.%Y}" if at else ""
        return format_html('<span title="Ahrefs, замер {}">{}</span>', when, value)

    @admin.display(description=f"трафик {label}", ordering=REGION_TRAFFIC)
    def region_traffic(obj: ProductSiteLatest) -> SafeString | str:
        return cell(getattr(obj, REGION_TRAFFIC, None), getattr(obj, REGION_AT, None))

    @admin.display(description=f"ключи {label}", ordering=REGION_KEYWORDS)
    def region_keywords(obj: ProductSiteLatest) -> SafeString | str:
        return cell(getattr(obj, REGION_KEYWORDS, None), getattr(obj, REGION_AT, None))

    return [region_traffic, region_keywords]


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
        # «Домены для Ahrefs» выбранного списка — ссылка над таблицей, без лишнего запроса.
        self.ahrefs_url = None
        for spec in self.filter_specs:
            value = spec.value() if isinstance(spec, SiteListFilter) else None
            if value and value.isdigit():
                self.ahrefs_url = reverse("admin:sites_sitelist_ahrefs", args=[int(value)])


@admin.register(ProductSiteLatest)
class ProductSiteLatestAdmin(NoDeleteAdmin):
    """Площадки продукта «на сегодня» — рабочий список вместо Excel.

    Строка — это представление, поэтому сами строки не правятся. Статус
    меняется по ссылке в колонке «статус», цены и заметки — в карточке
    (панель справа по щелчку на домене, E9-11), рабочая цена отмеченных
    строк — действиями.
    Всё, что в колонках, приходит одним запросом из представления, плюс
    один запрос — предложения строк страницы.
    """

    list_display = (
        "domain_link",
        "status_link",
        "dr_cell",
        "traffic_cell",
        "top_geo_cell",
        "gray_cell",
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
        RefsFilter,
        "status",
        OffersFilter,
        SellerFilter,
        RegionFilter,
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
        js = (Js("seo/country-picker.js"),)
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            "all": (Css("seo/offers.css"), Css("seo/gray.css"), Css("flags/sprite-hq.css"))
        }

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_fix_price_permission(self, request: HttpRequest) -> bool:
        # Строки — представление, их не правят; меняется площадка: её право.
        return bool(request.user.has_perm("sites.change_site"))

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        return export.ExportChangeList if export.is_export(request) else OffersChangeList

    def get_urls(self) -> list[URLPattern]:
        view = self.admin_site.admin_view
        own = [
            path(
                "export/<str:fmt>/",
                view(require_http_methods(["GET", "POST"])(self.export_view)),
                name=export.url_name(self),
            ),
        ]
        return own + super().get_urls()

    def export_choices(self, request: HttpRequest) -> list[export.Choice]:
        """Галочки окна выгрузки (тег `export_tools`); колонки региона — если он выбран."""
        return site_export.choices(_region(request))

    def export_view(self, request: HttpRequest, fmt: str) -> HttpResponse:
        """Окно «Выгрузить»: отобранное на экране или отмеченные строки (E1-06)."""
        region = _region(request)

        def build(
            changelist: ChangeList, queryset: models.QuerySet[Any], wanted: AbstractSet[str]
        ) -> tuple[list[export.Sheet], str]:
            name = f"Площадки {_chosen_product(changelist)} {timezone.localdate():%d.%m.%Y}"
            return [site_export.sheet(queryset, region, wanted)], name

        return export.export_view(self, request, fmt, self.export_choices(request), build)

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Any]:
        queryset: models.QuerySet[Any] = _with_gray_zone(super().get_queryset(request))
        region = _region(request)
        if region is None:
            return queryset
        # Последний замер страны — из v_site_country_latest; по одному значению на строку.
        latest = SiteCountryLatest.objects.filter(site_id=OuterRef("site_id"), country=region)
        annotated: models.QuerySet[Any] = queryset.annotate(
            **{
                REGION_TRAFFIC: Subquery(latest.values("organic_traffic")[:1]),
                REGION_KEYWORDS: Subquery(latest.values("total_keywords")[:1]),
                REGION_AT: Subquery(latest.values("checked_at")[:1]),
            }
        )
        return annotated

    def get_list_display(self, request: HttpRequest) -> Any:
        columns = list(self.list_display)
        region = _region(request)
        if region is not None:
            at = columns.index("top_geo_cell") + 1
            columns[at:at] = _region_columns(region)
        return columns

    def get_list_filter(self, request: HttpRequest) -> Any:
        filters = list(self.list_filter)
        region = _region(request)
        if region is not None:
            label = region.upper()
            at = filters.index(("organic_traffic", NumericRangeFilter)) + 1
            filters[at:at] = [
                _region_range(REGION_TRAFFIC, f"трафик {label}"),
                _region_range(REGION_KEYWORDS, f"ключи {label}"),
            ]
        return filters

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
        # Карточка открывается панелью справа (seo/panel.js, E9-11), с Ctrl и без
        # скрипта — отдельной страницей. Рядом — открыть сайт и скопировать домен.
        return format_html(
            '<a href="{}" data-panel title="Карточка площадки">{}</a>{}',
            card_url(obj.site_id),
            obj.domain,
            domain_tools_html(obj.domain),
        )

    @admin.display(description="статус", ordering="status")
    def status_link(self, obj: ProductSiteLatest) -> SafeString:
        # Решение открывается панелью справа (seo/panel.js, E9-11), с Ctrl и без
        # скрипта — полной формой.
        # data-seo-field — после записи в панели статус меняется здесь сразу.
        return format_html(
            '<a href="{}" data-panel="{}" title="Сменить статус" data-seo-field="status">{}</a>',
            reverse("admin:sites_productsite_change", args=[obj.pk]),
            reverse("admin:sites_productsite_decision", args=[obj.pk]),
            obj.get_status_display(),
        )

    @admin.display(description="DR", ordering="dr")
    def dr_cell(self, obj: ProductSiteLatest) -> SafeString:
        mark = seller_mark(obj.metrics_seller) if obj.metrics_trusted is False else ""
        return format_html("{}{}", "" if obj.dr is None else obj.dr, mark)

    @admin.display(description="трафик", ordering="organic_traffic")
    def traffic_cell(self, obj: ProductSiteLatest) -> str:
        return "" if obj.organic_traffic is None else f"{obj.organic_traffic}"

    @admin.display(description="топ регион", ordering="top_geo_traffic")
    def top_geo_cell(self, obj: ProductSiteLatest) -> SafeString | str:
        # Как Top Geo и Top Geo Traff в таблице: страна с наибольшим трафиком и её трафик.
        if not obj.top_geo:
            return ""
        when = f"{timezone.localtime(obj.top_geo_at):%d.%m.%Y}" if obj.top_geo_at else ""
        return format_html(
            '<span class="seo-geo" title="{}: больше всего трафика; замер {}">{}{} {}</span>',
            countries.name(obj.top_geo),
            when,
            countries.flag_html(obj.top_geo),
            obj.top_geo.upper(),
            "" if obj.top_geo_traffic is None else obj.top_geo_traffic,
        )

    @admin.display(description="серость", ordering="gray_ratio")
    def gray_cell(self, obj: ProductSiteLatest) -> SafeString | str:
        # Последний замер серости в Google (E2-06); зона — из get_queryset.
        if obj.gray_ratio is None:
            return ""
        zone = getattr(obj, "gray_zone", None)
        return format_html(
            '<span class="seo-gray-zone seo-gray-{}" title="{}">{}</span>',
            zone or "none",
            "Доля серых страниц в Google, последний замер"
            + (f": {gray_scan.ZONE_TITLES[zone]}" if zone else ""),
            gray_scan.percent_text(obj.gray_ratio),
        )

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
            '<a class="seo-notes" href="{}" data-panel title="{}">💬 {}</a>',
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


def _chosen_product(changelist: ChangeList) -> str:
    """Название продукта, выбранного в фильтре списка (без выбора — первый активный)."""
    for spec in changelist.filter_specs:
        if isinstance(spec, ProductFilter):
            value = spec.value()
            names = dict(spec.lookup_choices)
            return str(names.get(value, "")) if value else ""
    return ""


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


# Экраны «Загрузки» (E1-08) и «Ссылающиеся домены» (E1-09) — в своих модулях;
# регистрируются при загрузке этого.
from apps.sites import refdomain_admin, upload_admin  # noqa: E402, F401
