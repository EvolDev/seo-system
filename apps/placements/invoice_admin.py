"""Счета продавцов: «Работа → Счета» (E1-14, ADR-055).

Счёт открывается панелью справа поверх списка (E9-11). Размещения в нём —
строками с долями: пустая доля — сумма делится поровну, кнопка «Поделить
поровну» очищает все доли. Пока счёт не оплачен, строки правятся и убираются;
у оплаченного — только статус, дата и кто оплатил: чтобы поправить строки,
верните «Выставлен». Записали — «Заплачено» размещений пересчитывает
`invoices.sync_paid`.

Новый счёт на отмеченные размещения — действие «Счёт на отмеченные» в
«Размещениях»: форма открывается с ними (`?placements=1,2,3`), продавец,
сумма и доли подставляются, если у размещений они уже есть.
"""

from collections.abc import Set as AbstractSet
from datetime import date
from typing import Any, ClassVar

from django.contrib import admin
from django.contrib.admin.views.main import ChangeList
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_http_methods

from apps.placements import invoice_export, invoices
from apps.placements.forms import InvoiceForm, InvoiceItemForm, InvoiceItemFormSet
from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement
from apps.sites.display import domain_tools_html
from apps.sites.models import Product
from apps.sites.offers import money
from config import export
from config.admin import NoDeleteAdmin, TabularInline
from config.assets import Css, Js
from config.export import month_name

# Параметр адреса формы нового счёта: размещения через запятую.
PLACEMENTS_PARAM = "placements"
PAYMENT = "Оплата"
COMMENT = "Комментарий"
# Группы после строк счёта — шаблон admin/placements/invoice/change_form.html.
AFTER_ITEMS = (PAYMENT, COMMENT)
STATUS_CHIPS = {
    InvoiceStatus.ISSUED: "seo-chip seo-info",
    InvoiceStatus.PAID: "seo-chip seo-down",
}


def placement_ids(request: HttpRequest) -> list[int]:
    """Размещения из адреса формы нового счёта, по порядку и без повторов."""
    seen: dict[int, None] = {}
    for part in (request.GET.get(PLACEMENTS_PARAM) or "").split(","):
        if part.strip().isdigit():
            seen[int(part)] = None
    return list(seen)


def locked(invoice: Invoice | None) -> bool:
    """Оплаченный счёт: строки и сумма не правятся (ADR-055)."""
    return invoice is not None and invoice.pk is not None and invoice.status == InvoiceStatus.PAID


def user_name(user: Any) -> str:
    if user is None:
        return ""
    return str(user.get_full_name() or user.get_username())


class InvoiceItemInline(TabularInline):
    model = InvoiceItem
    form = InvoiceItemForm
    formset = InvoiceItemFormSet
    autocomplete_fields = ("placement",)
    verbose_name = "размещение"
    verbose_name_plural = "размещения в счёте"

    def get_extra(self, request: HttpRequest, obj: Any = None, **kwargs: Any) -> int:
        if obj is not None:
            return 0
        # Новый счёт: строки отмеченных размещений или одна пустая.
        return len(placement_ids(request)) or 1

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return ("placement", "amount_cents") if locked(obj) else ()

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return not locked(obj) and super().has_add_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Убрать размещение из счёта — правка счёта, пока он не оплачен (ADR-055).
        return not locked(obj) and super().has_delete_permission(request, obj)


class InvoiceProductFilter(admin.SimpleListFilter):
    """Продукт размещений счёта: счёт пачки может закрывать оба продукта."""

    title = "продукт"
    parameter_name = "product"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [(str(pk), name) for pk, name in Product.objects.values_list("pk", "name")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> Any:
        value = self.value()
        if not value or not value.isdigit():
            return queryset
        return queryset.filter(items__placement__product_id=int(value)).distinct()


class PaidMonthFilter(admin.SimpleListFilter):
    """Месяц оплаты: сколько и кому заплатили за месяц."""

    title = "оплачен"
    parameter_name = "paid"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        months = sorted(
            {
                day.replace(day=1)
                for day in Invoice.objects.values_list("paid_on", flat=True)
                if day is not None
            },
            reverse=True,
        )
        return [(f"{month:%Y-%m}", month_name(month)) for month in months]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> Any:
        value = self.value() or ""
        try:
            year, month = (int(part) for part in value.split("-"))
            start = date(year, month, 1)
        except ValueError:
            return queryset
        end = date(year + month // 12, month % 12 + 1, 1)
        return queryset.filter(paid_on__gte=start, paid_on__lt=end)


@admin.register(Invoice)
class InvoiceAdmin(NoDeleteAdmin):
    panel = True
    form = InvoiceForm
    inlines = (InvoiceItemInline,)
    list_display = (
        "seller_cell",
        "issued_day",
        "placements_cell",
        "amount_cell",
        "status_cell",
        "paid_cell",
        "pay_link",
        "number",
    )
    list_display_links = ("seller_cell",)
    list_filter = (
        "status",
        ("seller", admin.RelatedOnlyFieldListFilter),
        InvoiceProductFilter,
        PaidMonthFilter,
    )
    search_fields = ("seller__name", "number", "pay_url", "items__placement__site__domain")
    ordering = ("-issued_on", "-pk")
    autocomplete_fields = ("seller",)
    actions = ("mark_paid_action",)
    fieldsets = (
        (None, {"fields": ("seller", "status")}),
        ("Счёт", {"fields": (("amount_cents", "currency"), "pay_url", "number", "issued_on")}),
        (PAYMENT, {"fields": ("paid_on", "paid_by")}),
        (COMMENT, {"fields": ("comment",)}),
    )

    class Media:
        js = (Js("seo/invoices.js"),)
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            "all": (Css("seo/offers.css"), Css("seo/invoices.css"))
        }

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        if export.is_export(request):
            return export.ExportChangeList
        return super().get_changelist(request, **kwargs)

    def get_urls(self) -> list[URLPattern]:
        own = [
            path(
                "export/<str:fmt>/",
                self.admin_site.admin_view(require_http_methods(["GET", "POST"])(self.export_view)),
                name=export.url_name(self),
            ),
        ]
        return own + super().get_urls()

    def export_choices(self, request: HttpRequest) -> list[export.Choice]:
        """Галочки окна выгрузки (тег `export_tools`) — колонки листа «Счета»."""
        return invoice_export.choices()

    def export_view(self, request: HttpRequest, fmt: str) -> HttpResponse:
        """Лист «Счета»: окно «Выгрузить» — отобранное в списке или отмеченные строки."""

        def build(
            changelist: ChangeList, queryset: QuerySet[Any], wanted: AbstractSet[str]
        ) -> tuple[list[export.Sheet], str]:
            return invoice_export.sheets(queryset, wanted), invoice_export.file_name()

        return export.export_view(self, request, fmt, self.export_choices(request), build)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Invoice]:
        return (
            super()
            .get_queryset(request)
            .select_related("seller", "paid_by")
            .prefetch_related("items__placement__site", "items__placement__product")
        )

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        if not locked(obj):
            return super().get_fieldsets(request, obj)
        # Оплаченный: сумма — текстом, её не правят.
        return [
            (
                name,
                {
                    **options,
                    "fields": tuple(
                        "amount_text" if field == ("amount_cents", "currency") else field
                        for field in options["fields"]
                    ),
                },
            )
            for name, options in super().get_fieldsets(request, obj)
        ]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return ("seller", "amount_text") if locked(obj) else ()

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, Any]:
        """Новый счёт на отмеченные: продавец и сумма — если у размещений они общие."""
        initial: dict[str, Any] = dict(super().get_changeform_initial_data(request))
        initial.pop(PLACEMENTS_PARAM, None)
        placements = list(Placement.objects.filter(pk__in=placement_ids(request)))
        sellers = {placement.seller_id for placement in placements}
        if len(sellers) == 1 and None not in sellers:
            initial["seller"] = sellers.pop()
        currencies = {placement.currency for placement in placements}
        paid = [placement.price_paid_cents for placement in placements]
        if placements and None not in paid and len(currencies) == 1:
            initial["amount_cents"] = sum(value or 0 for value in paid)
            initial["currency"] = currencies.pop()
        return initial

    def get_formset_kwargs(
        self, request: HttpRequest, obj: Any, inline: Any, prefix: str
    ) -> dict[str, Any]:
        kwargs = super().get_formset_kwargs(request, obj, inline, prefix)
        ids = placement_ids(request)
        if request.method == "GET" and obj.pk is None and ids:
            placements = Placement.objects.in_bulk(ids)
            currencies = {placements[pk].currency for pk in ids if pk in placements}
            keep = len(currencies) == 1 and all(
                placements[pk].price_paid_cents is not None for pk in ids if pk in placements
            )
            kwargs["initial"] = [
                {
                    "placement": pk,
                    # Заплачено, вписанное до счёта, — долей; иначе поровну.
                    "amount_cents": placements[pk].price_paid_cents if keep else None,
                }
                for pk in ids
                if pk in placements
            ]
        return kwargs

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: Any = None,
    ) -> Any:
        context["after_items"] = AFTER_ITEMS
        context["can_split"] = not locked(obj)
        return super().render_change_form(request, context, add, change, form_url, obj)

    def panel_title(self, obj: Invoice) -> str:
        return invoices.label(obj)

    def save_model(self, request: HttpRequest, obj: Invoice, form: Any, change: bool) -> None:
        if obj.status == InvoiceStatus.PAID and obj.paid_by_id is None:
            obj.paid_by = request.user  # type: ignore[assignment]
        super().save_model(request, obj, form, change)

    def save_related(
        self, request: HttpRequest, form: Any, formsets: list[Any], change: bool
    ) -> None:
        # Строки в базе до записи — и те, что убрали: им «Заплачено» тоже пересчитать.
        before = set(form.instance.items.values_list("placement_id", flat=True))
        super().save_related(request, form, formsets, change)
        after = set(form.instance.items.values_list("placement_id", flat=True))
        invoices.sync_paid(before | after)

    @admin.action(description="Отметить оплаченными")
    def mark_paid_action(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        today = timezone.localdate()
        count = 0
        for invoice in queryset.filter(status=InvoiceStatus.ISSUED):
            invoice.status = InvoiceStatus.PAID
            invoice.paid_on = invoice.paid_on or today
            if invoice.paid_by_id is None:
                invoice.paid_by = request.user  # type: ignore[assignment]
            invoice.save(update_fields=["status", "paid_on", "paid_by", "updated_at"])
            count += 1
        skipped = queryset.count() - count
        self.message_user(request, f"Отмечено оплаченными: {count}.")
        if skipped:
            self.message_user(
                request, f"Не выставлены (оплачены или отменены), не тронуты: {skipped}."
            )

    # ---------- Колонки ----------

    @admin.display(description="продавец", ordering="seller__name")
    def seller_cell(self, obj: Invoice) -> str:
        return obj.seller.name

    @admin.display(description="выставлен", ordering="issued_on")
    def issued_day(self, obj: Invoice) -> str:
        return f"{obj.issued_on:%d.%m.%Y}"

    @admin.display(description="размещения")
    def placements_cell(self, obj: Invoice) -> SafeString | str:
        items = list(obj.items.all())
        if not items:
            return "—"
        return format_html_join(
            "",
            '<div class="seo-invoice-site"><a href="{}" data-panel title="Карточка площадки">{}</a>'
            '{} <span class="seo-sub">{} · {}</span></div>',
            (
                (
                    reverse("admin:sites_site_card", args=[item.placement.site_id]),
                    item.placement.site.domain,
                    domain_tools_html(item.placement.site.domain),
                    item.placement.product.name,
                    money(item.amount_cents, obj.currency),
                )
                for item in items
            ),
        )

    @admin.display(description="сумма", ordering="amount_cents")
    def amount_cell(self, obj: Invoice) -> str:
        return money(obj.amount_cents, obj.currency)

    @admin.display(description="сумма")
    def amount_text(self, obj: Invoice) -> str:
        return money(obj.amount_cents, obj.currency)

    @admin.display(description="статус", ordering="status")
    def status_cell(self, obj: Invoice) -> SafeString:
        # Цвета — пометки «Площадок» (seo/offers.css): оплачен — зелёный, ждёт — синий.
        kind = STATUS_CHIPS.get(obj.status, "seo-flat")
        return format_html(
            '<span class="{}" data-seo-field="status">{}</span>', kind, obj.get_status_display()
        )

    @admin.display(description="оплачен", ordering="paid_on")
    def paid_cell(self, obj: Invoice) -> SafeString | str:
        if obj.paid_on is None:
            return ""
        who = user_name(obj.paid_by)
        if not who:
            return f"{obj.paid_on:%d.%m.%Y}"
        return format_html('{}<div class="seo-sub">{}</div>', f"{obj.paid_on:%d.%m.%Y}", who)

    @admin.display(description="оплата")
    def pay_link(self, obj: Invoice) -> SafeString | str:
        if not obj.pay_url:
            return ""
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer" data-no-soft'
            ' title="Открыть страницу оплаты в новой вкладке">открыть ↗</a>',
            obj.pay_url,
        )
