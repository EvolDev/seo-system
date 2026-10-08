"""Формы размещения и счёта под удобство (E9-11, ADR-048) — в панели и на полной странице.

Статус — кнопками по порядку: путь заявки, затем отказ и отмена. Кнопка
«Заявка отправлена» ставит сегодняшний день в пустую дату заявки,
«Опубликовано» — в пустую дату публикации (seo/widgets.js). Даты — день без
времени, «Заплачено» — сумма в валюте, а не центы (config/forms.py).

Счёт (E1-14, ADR-055): статус кнопками, «Оплачен» ставит сегодняшний день в
пустую дату оплаты; доли строк — `apps.placements.invoices.shares`.

Ссылка размещения (E3-05, ADR-059): анкор — одно поле с поиском по анкорам
продукта (seo/anchors.js), «куда ведёт» подставляется из анкора и правится;
текст и тип анкора берутся из него, номер ссылки ставится сам.
"""

from typing import Any, ClassVar

from django import forms
from django.db.models import Max, Q
from django.forms.models import BaseInlineFormSet
from django.utils import timezone

from apps.keywords.models import NAKED_TYPES, AnchorType, Keyword
from apps.placements import invoices
from apps.placements.models import (
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    Placement,
    PlacementLink,
    PlacementStatus,
)
from apps.sites.offers import CURRENCIES
from config.forms import ChoiceButtons, DayField, DayFieldsForm, DayInput, MoneyField

# Какую дату ставит кнопка статуса, если поле пустое.
STATUS_FILLS: dict[str, str] = {
    PlacementStatus.ORDERED: "ordered_at",
    PlacementStatus.PLACED: "published_at",
}


class PlacementForm(DayFieldsForm):
    # Подписи — с большой буквы, как Django пишет подписи полей модели.
    ordered_at = DayField(label="Заявка отправлена", required=False)
    published_at = DayField(label="Опубликовано", required=False)
    price_paid_cents = MoneyField(label="Заплачено", required=False)
    currency = forms.ChoiceField(label="Валюта", required=False)

    class Meta:
        model = Placement
        fields = "__all__"
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "status": ChoiceButtons(
                rows=(PlacementStatus.IN_WORK, PlacementStatus.DISCARDED), fills=STATUS_FILLS
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _currency_choices(self, blank=True)

    def clean_currency(self) -> str | None:
        value: str = self.cleaned_data.get("currency") or ""
        return value or None


def _currency_choices(form: forms.ModelForm, *, blank: bool) -> None:  # type: ignore[type-arg]
    """Валюты на выбор; валюта не из списка (так записал импорт) остаётся выбранной."""
    current = form.instance.currency
    codes = list(CURRENCIES) if current in CURRENCIES or not current else [*CURRENCIES, current]
    field = form.fields.get("currency")
    if isinstance(field, forms.ChoiceField):
        choices = [(code, code) for code in codes]
        field.choices = [("", "—"), *choices] if blank else choices
        field.initial = current or "EUR"


class InvoiceForm(forms.ModelForm):  # type: ignore[type-arg]
    amount_cents = MoneyField(label="Сумма")
    currency = forms.ChoiceField(label="Валюта")
    issued_on = forms.DateField(label="Выставлен", widget=DayInput)
    paid_on = forms.DateField(label="Оплачен", widget=DayInput, required=False)

    class Meta:
        model = Invoice
        fields = (
            "seller",
            "status",
            "amount_cents",
            "currency",
            "pay_url",
            "number",
            "issued_on",
            "paid_on",
            "paid_by",
            "comment",
        )
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "status": ChoiceButtons(fills={InvoiceStatus.PAID: "paid_on"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _currency_choices(self, blank=False)
        if "issued_on" in self.fields and self.instance.pk is None:
            self.fields["issued_on"].initial = timezone.localdate

    def clean_amount_cents(self) -> int:
        value: int = self.cleaned_data["amount_cents"]
        if value <= 0:
            raise forms.ValidationError("Сумма счёта — больше нуля.")
        return value

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if cleaned is None:
            return None
        # «Оплачен» без даты — сегодня: кнопка статуса ставит её и в браузере.
        if cleaned.get("status") == InvoiceStatus.PAID and not cleaned.get("paid_on"):
            cleaned["paid_on"] = timezone.localdate()
        return cleaned


class InvoiceItemForm(forms.ModelForm):  # type: ignore[type-arg]
    amount_cents = MoneyField(
        label="Доля",
        required=False,
        help_text="Пусто — сумма делится поровну.",
    )

    class Meta:
        model = InvoiceItem
        fields = ("placement", "amount_cents")


class InvoiceItemFormSet(BaseInlineFormSet):  # type: ignore[type-arg]
    """Строки счёта: пустые доли делят остаток поровну, сумма долей — сумма счёта.

    Сумму и валюту берём у счёта (`self.instance`): админка строит строки
    после проверки формы счёта. Форма счёта с ошибками — строки не сверяем.
    """

    def clean(self) -> None:
        if any(self.errors):
            return
        invoice: Invoice = self.instance
        live = [
            form
            for form in self.forms
            if "placement" in form.fields
            and form.cleaned_data.get("placement") is not None
            and not form.cleaned_data.get("DELETE")
        ]
        placements = [form.cleaned_data["placement"] for form in live]
        # Раньше штатной проверки уникальности: её подпись про «повторяющееся значение».
        if len({placement.pk for placement in placements}) != len(placements):
            raise forms.ValidationError("Размещение в счёте дважды — оставьте одну строку.")
        super().clean()
        if invoice.amount_cents is None or invoice.seller_id is None:
            return
        errors = invoices.item_errors(
            invoice, placements, currency=invoice.currency, seller_id=invoice.seller_id
        )
        if errors:
            raise forms.ValidationError(errors)
        unchanged = not any(form.has_changed() for form in live) and not self.deleted_forms
        if unchanged and not _total_changed(invoice):
            return
        given = [form.cleaned_data.get("amount_cents") for form in live]
        try:
            values = invoices.shares(invoice.amount_cents, given, invoice.currency)
        except invoices.ShareError as error:
            raise forms.ValidationError(str(error)) from error
        for form, value in zip(live, values, strict=True):
            form.cleaned_data["amount_cents"] = value
            form.instance.amount_cents = value


def _total_changed(invoice: Invoice) -> bool:
    """Сумма или валюта счёта не те, что в базе: доли надо сверить заново."""
    if invoice.pk is None:
        return True
    before = Invoice.objects.filter(pk=invoice.pk).values_list("amount_cents", "currency").first()
    return before != (invoice.amount_cents, invoice.currency)


class AnchorSelect(forms.Select):
    """Выбор анкора: обычный <select>, seo/anchors.js превращает его в поле с поиском.

    У каждого варианта — продукт, адрес и тип страницы: поле показывает
    анкоры продукта размещения и подставляет адрес в «Куда ведёт». `legacy` —
    текст старой ссылки без анкора из списка: остаётся выбранным, пока не
    выберут анкор.
    """

    legacy: str = ""

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__({"data-anchor-select": "", **(attrs or {})})

    def create_option(
        self,
        name: str,
        value: Any,
        label: Any,
        selected: Any,
        index: int,
        subindex: int | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        keyword = getattr(value, "instance", None)
        if isinstance(keyword, Keyword):
            option["attrs"].update(
                {
                    "data-product": str(keyword.product_id),
                    "data-url": keyword.target_url,
                    "data-type": keyword.page_type or "",
                    "data-naked": "1" if keyword.anchor_type in NAKED_TYPES else "",
                }
            )
        elif value == "" and self.legacy:
            option["label"] = f"«{self.legacy}» — нет в анкорах"
            option["attrs"]["data-legacy"] = "1"
        return option


class PlacementLinkForm(forms.ModelForm):  # type: ignore[type-arg]
    keyword = forms.ModelChoiceField(
        label="Анкор",
        queryset=Keyword.objects.none(),
        required=False,
        widget=AnchorSelect,
        empty_label="— выберите анкор —",
    )
    target_url = forms.CharField(
        label="Куда ведёт",
        required=False,
        max_length=2000,
        widget=forms.URLInput(attrs={"data-anchor-url": "", "class": "vURLField"}),
        help_text="Подставляется из анкора; если адрес другой — впишите свой.",
    )

    class Meta:
        model = PlacementLink
        fields = ("keyword", "target_url")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["keyword"]
        assert isinstance(field, forms.ModelChoiceField)
        current = self.instance.keyword_id
        # Активные анкоры всех продуктов: поле само оставит анкоры продукта
        # размещения; выключенный анкор этой ссылки — тоже, иначе он пропал бы.
        field.queryset = (
            Keyword.objects.filter(Q(is_active=True) | Q(pk=current))
            .only("pk", "keyword", "product_id", "target_url", "page_type", "anchor_type")
            .order_by("product_id", "keyword")
        )
        if self.instance.pk is not None and current is None and self.instance.anchor:
            widget = field.widget
            assert isinstance(widget, AnchorSelect)
            widget.legacy = self.instance.anchor
            field.empty_label = ""

    def clean(self) -> dict[str, Any] | None:
        data = super().clean()
        if data is None:
            return None
        keyword: Keyword | None = data.get("keyword")
        url = (data.get("target_url") or "").strip()
        if keyword is not None:
            self.instance.anchor = keyword.keyword
            self.instance.anchor_type = keyword.anchor_type or AnchorType.EXACT
            if not url:
                url = keyword.target_url
        elif not (self.instance.pk is not None and self.instance.anchor):
            if url:
                self.add_error("keyword", "Выберите анкор.")
            return data
        if url and not url.startswith(("http://", "https://")):
            self.add_error("target_url", "Адрес страницы целиком: https://…")
        elif not url:
            self.add_error("target_url", "Впишите, куда ведёт ссылка.")
        data["target_url"] = url
        return data


class PlacementLinkFormSet(BaseInlineFormSet):  # type: ignore[type-arg]
    """Ссылки статьи: номер новой ссылки — следующий за последним у размещения."""

    def save_new(self, form: forms.ModelForm, commit: bool = True) -> Any:  # type: ignore[type-arg]
        link = form.save(commit=False)
        link.placement = self.instance
        if link.link_index is None:
            last = PlacementLink.objects.filter(placement=self.instance).aggregate(
                last=Max("link_index")
            )["last"]
            link.link_index = (last or 0) + 1
        if commit:
            link.save()
        return link
