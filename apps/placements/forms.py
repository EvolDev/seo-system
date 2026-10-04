"""Формы размещения и счёта под удобство (E9-11, ADR-048) — в панели и на полной странице.

Статус — кнопками по порядку: путь заявки, затем отказ и отмена. Кнопка
«Заявка отправлена» ставит сегодняшний день в пустую дату заявки,
«Опубликовано» — в пустую дату публикации (seo/widgets.js). Даты — день без
времени, «Заплачено» — сумма в валюте, а не центы (config/forms.py).

Счёт (E1-14, ADR-055): статус кнопками, «Оплачен» ставит сегодняшний день в
пустую дату оплаты; доли строк — `apps.placements.invoices.shares`.
"""

from typing import Any, ClassVar

from django import forms
from django.forms.models import BaseInlineFormSet
from django.utils import timezone

from apps.placements import invoices
from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement, PlacementStatus
from apps.sites.offers import CURRENCIES
from config.forms import ChoiceButtons, DayField, DayFieldsForm, DayInput, MoneyField

# Какую дату ставит кнопка статуса, если поле пустое.
STATUS_FILLS: dict[str, str] = {
    PlacementStatus.ORDERED: "ordered_at",
    PlacementStatus.PUBLISHED: "published_at",
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
            "status": ChoiceButtons(rows=(PlacementStatus.REJECTED,), fills=STATUS_FILLS),
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
