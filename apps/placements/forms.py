"""Формы размещения и счёта под удобство (E9-11, ADR-048) — в панели и на полной странице.

Статус — кнопками по порядку: путь заявки, затем отказ и отмена. Кнопка
«Заявка отправлена» ставит сегодняшний день в пустую дату заявки,
«Опубликовано» — в пустую дату публикации (seo/widgets.js). Даты — день без
времени, «Заплачено» — сумма в валюте, а не центы (config/forms.py).

Счёт (E1-14, ADR-055): статус кнопками, «Оплачен» ставит сегодняшний день в
пустую дату оплаты; доли строк — `apps.placements.invoices.shares`.

Статья (ADR-072): копирайтер — список работающих с их ставкой в данных
вариантов; выбор подставляет ставку в пустую «Цену копирайтера»
(seo/copywriter-pick.js), без скрипта — при сохранении (`clean`).

Ссылка размещения (E3-05, ADR-059, ADR-069): анкор — одно поле с поиском по
анкорам продукта (seo/anchors.js), «куда ведёт» подставляется из анкора и
правится; текст на сайте — свой (разбавленный анкор) или текст анкора, вид
ссылки — по тексту или выбором человека; номер ссылки ставится сам.
"""

from typing import Any, ClassVar

from django import forms
from django.db.models import Max, Q
from django.forms.models import BaseInlineFormSet
from django.urls import reverse
from django.utils import timezone

from apps.content.models import Copywriter
from apps.keywords import matching
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


class CopywriterSelect(forms.Select):
    """Копирайтер со ставкой в данных варианта: скрипт подставит её в цену статьи."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rates: dict[str, tuple[int | None, str]] = {}

    def create_option(self, name: str, value: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        option = super().create_option(name, value, *args, **kwargs)
        rate = self.rates.get(str(value))
        if rate is not None and rate[0] is not None:
            units, rest = divmod(rate[0], 100)
            option["attrs"]["data-rate"] = f"{units}" if rest == 0 else f"{units},{rest:02d}"
            option["attrs"]["data-currency"] = rate[1]
        return option


class PlacementForm(DayFieldsForm):
    # Подписи — с большой буквы, как Django пишет подписи полей модели.
    ordered_at = DayField(label="Заявка отправлена", required=False)
    published_at = DayField(label="Опубликовано", required=False)
    price_paid_cents = MoneyField(label="Заплачено", required=False)
    currency = forms.ChoiceField(label="Валюта", required=False)
    copywriter_cents = MoneyField(
        label="Цена копирайтера",
        required=False,
        help_text="За статью; при выборе копирайтера — его ставка.",
    )
    copywriter_currency = forms.ChoiceField(label="Валюта", required=False)

    class Meta:
        model = Placement
        fields = "__all__"
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "status": ChoiceButtons(
                rows=(PlacementStatus.IN_WORK, PlacementStatus.DISCARDED), fills=STATUS_FILLS
            ),
            "copywriter": CopywriterSelect(attrs={"data-copywriter": ""}),
            "draft_url": forms.TextInput(
                attrs={"class": "vTextField", "placeholder": "https://docs.google.com/…"}
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _currency_choices(self, blank=True)
        _currency_choices(self, blank=True, name="copywriter_currency")
        field = self.fields.get("copywriter")
        if isinstance(field, forms.ModelChoiceField):
            # Не работающих не предлагаем, но уже назначенный остаётся в списке.
            current = self.instance.copywriter_id
            choices = Copywriter.objects.filter(
                Q(is_active=True) | Q(pk=current) if current else Q(is_active=True)
            ).order_by("name")
            field.queryset = choices
            # В админке список обёрнут кнопками «добавить / изменить» — ставки нужны
            # самому списку внутри обёртки.
            widget = getattr(field.widget, "widget", field.widget)
            if isinstance(widget, CopywriterSelect):
                widget.rates = {
                    str(pk): (rate, currency)
                    for pk, rate, currency in choices.values_list("pk", "rate_cents", "currency")
                }

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if cleaned is None:
            return None
        copywriter = cleaned.get("copywriter")
        if copywriter is not None and cleaned.get("copywriter_cents") is None:
            # Цена не вписана — ставка копирайтера; без скрипта в браузере тоже.
            cleaned["copywriter_cents"] = copywriter.rate_cents
            if copywriter.rate_cents is not None:
                cleaned["copywriter_currency"] = copywriter.currency
        if cleaned.get("copywriter_cents") is None:
            # Без суммы валюта ни к чему: «EUR» по умолчанию не записываем.
            cleaned["copywriter_currency"] = None
        elif not cleaned.get("copywriter_currency"):
            cleaned["copywriter_currency"] = copywriter.currency if copywriter else "EUR"
        return cleaned

    def clean_copywriter_currency(self) -> str | None:
        value: str = self.cleaned_data.get("copywriter_currency") or ""
        return value or None

    def clean_link_type(self) -> str | None:
        # Пустой выбор тоже решение человека: автоматическая подстановка его не отменяет.
        self.instance.link_type_manual = True
        return self.cleaned_data.get("link_type") or None

    def clean_currency(self) -> str | None:
        value: str = self.cleaned_data.get("currency") or ""
        return value or None


def _currency_choices(
    form: forms.ModelForm,  # type: ignore[type-arg]
    *,
    blank: bool,
    name: str = "currency",
) -> None:
    """Валюты на выбор; валюта не из списка (так записал импорт) остаётся выбранной."""
    current = getattr(form.instance, name)
    codes = list(CURRENCIES) if current in CURRENCIES or not current else [*CURRENCIES, current]
    field = form.fields.get(name)
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
    """Ссылка размещения: ключ, текст на сайте, вид ссылки и куда ведёт (ADR-069).

    Текст пустой — берётся текст анкора, ссылка прямая. Свой текст с ключом
    внутри («Convertio's mp4 to mp3 converter») — разбавленная; ключ не выбран —
    подставляется найденный во фразе (`apps.keywords.matching`). Вид «по
    тексту» определяет система; ключа во фразе нет — вид выбирает человек.
    Текст и адрес сохранённой ссылки форма сама не меняет.
    """

    keyword = forms.ModelChoiceField(
        label="Анкор",
        queryset=Keyword.objects.none(),
        required=False,
        widget=AnchorSelect,
        empty_label="— выберите анкор —",
    )
    text = forms.CharField(
        label="Текст на сайте",
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"data-anchor-text": "", "autocomplete": "off"}),
        help_text="Пусто — текст анкора. С брендом или словами вокруг ключа — разбавленная ссылка.",
    )
    kind = forms.ChoiceField(
        label="Вид ссылки",
        required=False,
        choices=[("", "По тексту"), *AnchorType.choices],
        widget=forms.Select(attrs={"data-anchor-kind": ""}),
        help_text="«По тексту» — определит система. Ключа во фразе нет — выберите сами.",
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

    def __init__(self, *args: Any, product_id: int | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.product_id = product_id
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
        field.widget.attrs["data-match-url"] = reverse("admin:keywords_anchor_match")
        link = self.instance
        if link.pk is not None and link.anchor:
            self.fields["text"].initial = link.anchor
            # Записанный вид показываем, только если он не тот, что дал бы текст:
            # иначе «по тексту», и правка текста пересчитает вид.
            keyword = link.keyword if current else None
            auto = (
                matching.kind(link.anchor, keyword.keyword, keyword.anchor_type)
                if keyword is not None
                else None
            )
            if link.anchor_type and link.anchor_type != auto:
                self.fields["kind"].initial = link.anchor_type
        if link.pk is not None and current is None and link.anchor:
            widget = field.widget
            assert isinstance(widget, AnchorSelect)
            widget.legacy = link.anchor
            field.empty_label = ""

    def clean(self) -> dict[str, Any] | None:
        data = super().clean()
        if data is None:
            return None
        keyword: Keyword | None = data.get("keyword")
        url = (data.get("target_url") or "").strip()
        text = " ".join((data.get("text") or "").split())
        chosen = data.get("kind") or None
        product_id = self._product_id()
        if keyword is not None and product_id and keyword.product_id != product_id:
            self.add_error("keyword", "Анкор другого продукта — выберите анкор этого размещения.")
            return data
        # Ключ по фразе — только когда фразу ввели: сохранённую ссылку без ключа
        # обычное сохранение молча не привязывает.
        typed = self.instance.pk is None or "text" in self.changed_data
        if keyword is None and text and product_id and typed:
            found = matching.find(text, matching.candidates(product_id), url=url or None)
            if found.ambiguous:
                names = ", ".join(f"«{c.keyword}»" for c in found.candidates[:4])
                self.add_error(
                    "keyword", f"Во фразе ключи на разные страницы: {names} — выберите анкор."
                )
                return data
            if found.keyword is not None:
                keyword = Keyword.objects.get(pk=found.keyword.id)
                data["keyword"] = keyword
                self.instance.keyword = keyword
        if keyword is not None:
            text = text or keyword.keyword
            kind = chosen or matching.kind(text, keyword.keyword, keyword.anchor_type)
            if kind is None:
                self.add_error(
                    "kind", f"Ключа «{keyword.keyword}» во фразе нет — выберите вид ссылки."
                )
            self.instance.anchor = text
            self.instance.anchor_type = kind
            if not url:
                url = keyword.target_url
        elif text and (self.instance.pk is None or typed):
            # Фраза без ключа из списка: ссылка сохраняется как есть, в долях —
            # отдельной строкой «без анкора из списка».
            self.instance.anchor = text
            self.instance.anchor_type = chosen
        elif self.instance.pk is not None and self.instance.anchor:
            if chosen:
                self.instance.anchor_type = chosen
        else:
            if url:
                self.add_error("keyword", "Выберите анкор или впишите текст ссылки.")
            return data
        if url and not url.startswith(("http://", "https://")):
            self.add_error("target_url", "Адрес страницы целиком: https://…")
        elif not url:
            self.add_error("target_url", "Впишите, куда ведёт ссылка.")
        data["target_url"] = url
        return data

    def _product_id(self) -> int | None:
        """Продукт размещения: его передаёт набор ссылок, у нового размещения — из формы."""
        if self.product_id is not None:
            return self.product_id
        try:
            placement: Placement = self.instance.placement
        except Placement.DoesNotExist:
            return None
        return placement.product_id


class PlacementLinkFormSet(BaseInlineFormSet):  # type: ignore[type-arg]
    """Ссылки статьи: номер новой ссылки — следующий за последним у размещения."""

    def get_form_kwargs(self, index: int | None) -> dict[str, Any]:
        # Продукт размещения — для поиска ключа во фразе и проверки анкора.
        return {**super().get_form_kwargs(index), "product_id": self.instance.product_id}

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
