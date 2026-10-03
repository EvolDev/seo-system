"""Форма размещения под удобство (E9-11, ADR-048) — в панели и на полной странице.

Статус — кнопками по порядку: путь заявки, затем отказ и отмена. Кнопка
«Заявка отправлена» ставит сегодняшний день в пустую дату заявки,
«Опубликовано» — в пустую дату публикации (seo/widgets.js). Даты — день без
времени, «Заплачено» — сумма в валюте, а не центы (config/forms.py).
"""

from typing import Any, ClassVar

from django import forms

from apps.placements.models import Placement, PlacementStatus
from apps.sites.offers import CURRENCIES
from config.forms import ChoiceButtons, DayField, DayFieldsForm, MoneyField

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
        # Валюта не из списка (так записал импорт) остаётся выбранной.
        current = self.instance.currency
        codes = list(CURRENCIES) if current in CURRENCIES or not current else [*CURRENCIES, current]
        field = self.fields.get("currency")
        if isinstance(field, forms.ChoiceField):
            field.choices = [("", "—"), *((code, code) for code in codes)]
            field.initial = current or "EUR"

    def clean_currency(self) -> str | None:
        value: str = self.cleaned_data.get("currency") or ""
        return value or None
