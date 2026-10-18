"""Копирайтеры (Справочники → Копирайтеры, ADR-072): список и карточка.

Список — имя, ставка за статью, сколько статей пишется сейчас и сколько
размещено, сколько заработано. Карточка — панелью справа: поля копирайтера и
его размещения в «Написании статьи» и «Размещено»: площадка, продукт, черновик,
статья, цена, даты. Назначают копирайтера в панели размещения.
"""

from decimal import Decimal
from typing import Any, ClassVar

from django import forms
from django.contrib import admin
from django.db import models
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Func, Q, Sum
from django.db.models.expressions import CombinedExpression
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import SafeString

from apps.content.models import Copywriter
from apps.placements.models import Placement, PlacementStatus
from apps.sites.offers import CURRENCIES, money
from config.admin import RecordAdmin
from config.assets import Css
from config.forms import MoneyField

# Карточка показывает статьи, которые пишутся и которые уже размещены.
CARD_STATUSES = (PlacementStatus.WRITING, PlacementStatus.PLACED)


class CopywriterForm(forms.ModelForm):  # type: ignore[type-arg]
    rate_cents = MoneyField(label="Ставка за статью", required=False)
    currency = forms.ChoiceField(
        label="Валюта", choices=[(code, code) for code in CURRENCIES], initial="EUR"
    )

    class Meta:
        model = Copywriter
        fields = ("name", "rate_cents", "currency", "contacts", "notes", "is_active")
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "contacts": forms.TextInput(attrs={"class": "vTextField"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


def _eur(cents_field: str, currency_field: str) -> "ExpressionWrapper[CombinedExpression]":
    """Сумма в евро по последнему курсу ЕЦБ — функция базы `eur_rate`."""
    rate = Func(F(currency_field), function="eur_rate", output_field=DecimalField())
    return ExpressionWrapper(F(cents_field) / rate, output_field=DecimalField())


@admin.register(Copywriter)
class CopywriterAdmin(RecordAdmin):
    panel = True
    form = CopywriterForm
    list_display = (
        "name",
        "rate_cell",
        "writing_cell",
        "placed_cell",
        "earned_cell",
        "is_active",
        "contacts",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "contacts")
    ordering = ("-is_active", "name")
    fields = (
        "name",
        ("rate_cents", "currency"),
        "contacts",
        "notes",
        "is_active",
        "placements_block",
    )
    readonly_fields = ("placements_block",)

    class Media:
        css: ClassVar[dict[str, tuple[Css, ...]]] = {"all": (Css("seo/copywriters.css"),)}

    def get_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        fields = tuple(super().get_fields(request, obj))
        # У нового копирайтера статей ещё нет — блок не рисуем.
        return fields if obj is not None else tuple(f for f in fields if f != "placements_block")

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[Copywriter]:
        # Счётчики и заработок — одним запросом на страницу, а не запросом на строку.
        placed = Q(placements__status=PlacementStatus.PLACED)
        queryset: models.QuerySet[Copywriter] = super().get_queryset(request)
        return queryset.annotate(
            writing_total=Count("placements", filter=Q(placements__status=PlacementStatus.WRITING)),
            placed_total=Count("placements", filter=placed),
            earned_eur=Sum(
                _eur("placements__copywriter_cents", "placements__copywriter_currency"),
                filter=placed,
            ),
        )

    @admin.display(description="ставка за статью", ordering="rate_cents")
    def rate_cell(self, obj: Copywriter) -> str:
        return money(obj.rate_cents, obj.currency) or "—"

    @admin.display(description="пишет сейчас", ordering="writing_total")
    def writing_cell(self, obj: Any) -> int:
        return int(obj.writing_total)

    @admin.display(description="размещено", ordering="placed_total")
    def placed_cell(self, obj: Any) -> int:
        return int(obj.placed_total)

    @admin.display(description="заработал, ≈ €", ordering="earned_eur")
    def earned_cell(self, obj: Any) -> str:
        value: Decimal | None = obj.earned_eur
        return money(round(value), "EUR") if value else "—"

    @admin.display(description="статьи")
    def placements_block(self, obj: Copywriter | None) -> SafeString | str:
        """Размещения копирайтера: пишутся и размещены — с черновиком и статьёй."""
        if obj is None or obj.pk is None:
            return "—"
        rows = list(
            Placement.objects.filter(copywriter=obj, status__in=CARD_STATUSES)
            .select_related("site", "product")
            .order_by(
                F("published_at").desc(nulls_last=True),
                F("ordered_at").desc(nulls_last=True),
                "pk",
            )
        )
        # Сначала те, что пишутся сейчас, — с ними работают; дальше размещённые.
        rows.sort(key=lambda row: row.status != PlacementStatus.WRITING)
        current = timezone.localdate().replace(day=1)
        sums: dict[str, int] = {}
        month: dict[str, int] = {}
        for row in rows:
            if row.status != PlacementStatus.PLACED or row.copywriter_cents is None:
                continue
            currency = row.copywriter_currency or "EUR"
            sums[currency] = sums.get(currency, 0) + row.copywriter_cents
            if row.published_at and timezone.localdate(row.published_at) >= current:
                month[currency] = month.get(currency, 0) + row.copywriter_cents
        writing = [row for row in rows if row.status == PlacementStatus.WRITING]
        return render_to_string(
            "admin/content/copywriter/placements.html",
            {
                "rows": [
                    {
                        "row": row,
                        "url": reverse("admin:placements_placement_change", args=[row.pk]),
                        "writing": row.status == PlacementStatus.WRITING,
                        "status": PlacementStatus(row.status).label,
                        "price": money(row.copywriter_cents, row.copywriter_currency or "EUR"),
                    }
                    for row in rows
                ],
                "writing": len(writing),
                "writing_sum": _sums(
                    (row.copywriter_currency or "EUR", row.copywriter_cents)
                    for row in writing
                    if row.copywriter_cents is not None
                ),
                "placed": len(rows) - len(writing),
                "several_products": len({row.product_id for row in rows}) > 1,
                "earned": _text(sums),
                "earned_month": _text(month),
            },
        )


def _sums(pairs: Any) -> str:
    total: dict[str, int] = {}
    for currency, cents in pairs:
        total[currency] = total.get(currency, 0) + cents
    return _text(total)


def _text(sums: dict[str, int]) -> str:
    """«€1 200 + $300»; пусто — «—»."""
    return " + ".join(money(cents, currency) for currency, cents in sorted(sums.items())) or "—"
