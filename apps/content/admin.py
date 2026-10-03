"""Админка настроек: общие — раздел «Настройки», локальные — на странице продукта.

Общие значения не удаляются, как и всё остальное. Локальное значение
продукта удаляется (ADR-035): без него продукт снова работает по общему.
Форма и таблица для страницы продукта живут здесь, а подключает их
`apps.sites.admin.ProductAdmin`.
"""

import json
from typing import Any

from django import forms
from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from apps.content.domain_settings import (
    PRODUCT_KEYS,
    SETTING_KEYS,
    get_setting,
    set_product_setting,
    validate_setting,
)
from apps.content.models import DomainSetting
from apps.sites.domains import normalize_domain
from apps.sites.models import Product
from config.admin import NoDeleteAdmin, TabularInline

# Поля ценового ориентира на странице продукта → ключи внутри PRICE_REFERENCE.
PRICE_FIELDS = {
    "price_total_eur": "total_eur",
    "price_writing_eur": "writing_eur",
    "price_announce_eur": "announce_eur",
}
# Поля-списки на странице продукта → ключ настройки.
LIST_FIELDS = {
    "project_topics": "PROJECT_TOPICS",
    "tool_categories": "TOOL_CATEGORIES",
    "authority_domains": "AUTHORITY_DOMAINS",
}
PRODUCT_SETTING_FIELDS = (*PRICE_FIELDS, *LIST_FIELDS)


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _lines(text: str) -> list[str]:
    """Строки без пустых и повторов, в том порядке, в каком их ввели."""
    result: list[str] = []
    for line in text.splitlines():
        item = line.strip()
        if item and item not in result:
            result.append(item)
    return result


class ProductSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Продукт и его основные настройки — блок «Настройки продукта».

    Пустое поле — у продукта нет своего значения: действует общее, а где
    общего нет, признак не считается.
    """

    price_total_eur = forms.IntegerField(
        label="Ценовой ориентир, EUR",
        min_value=0,
        required=False,
        help_text="С ним сравнивается размещение + анонс. Написание не входит никогда.",
    )
    price_writing_eur = forms.IntegerField(
        label="Порог написания, EUR",
        min_value=0,
        required=False,
        help_text=(
            "Написание дешевле порога или равно ему — пишет площадка, дороже — пишем мы."
            " Без порога плановые расходы по площадкам не считаются."
        ),
    )
    price_announce_eur = forms.IntegerField(label="Цена анонса, EUR", min_value=0, required=False)
    project_topics = forms.CharField(
        label="Тематики продукта",
        widget=forms.Textarea(attrs={"rows": 6}),
        required=False,
        help_text="По одной на строку, в написании Collaborator.",
    )
    tool_categories = forms.CharField(
        label="Разделы сайта",
        widget=forms.Textarea(attrs={"rows": 5}),
        required=False,
        help_text="По одному на строку: Main, Video… Это допустимые разделы у ключей.",
    )
    authority_domains = forms.CharField(
        label="Белый список доменов",
        widget=forms.Textarea(attrs={"rows": 6}),
        required=False,
        help_text=(
            "Авторитетные источники для ссылок из статьи, по одному домену на строку."
            " Список полный: общий к нему не добавляется."
        ),
    )

    class Meta:
        model = Product
        fields = ("name", "domain", "is_active")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        local_price = self._local("PRICE_REFERENCE")
        for field, part in PRICE_FIELDS.items():
            if isinstance(local_price, dict):
                self.fields[field].initial = local_price.get(part)
        for field, key in LIST_FIELDS.items():
            local = self._local(key)
            if isinstance(local, list):
                self.fields[field].initial = "\n".join(str(item) for item in local)
        # Подсказка, что действует при пустом поле, — только если есть общее.
        general = {key: get_setting(key, None) for key in PRODUCT_KEYS}
        if general["PRICE_REFERENCE"] is not None and local_price is None:
            self.fields[
                "price_writing_eur"
            ].help_text += f" Сейчас действует общее значение: {_show(general['PRICE_REFERENCE'])}."
        for field, key in LIST_FIELDS.items():
            if general[key] is not None:
                self.fields[field].help_text += f" Пусто — общее: {_show(general[key])}."

    def _local(self, key: str) -> Any | None:
        if not self.instance.pk:
            return None
        return (
            DomainSetting.objects.filter(key=key, product_id=self.instance.pk)
            .values_list("value", flat=True)
            .first()
        )

    def clean_authority_domains(self) -> str:
        domains: list[str] = []
        for line in _lines(self.cleaned_data["authority_domains"]):
            try:
                domain = normalize_domain(line)
            except ValueError as error:
                raise forms.ValidationError(f"Не похоже на домен: «{line}».") from error
            if domain not in domains:
                domains.append(domain)
        return "\n".join(domains)

    def save_settings(self, product: Product) -> None:
        """Записывает настройки продукта; пустое поле удаляет его значение."""
        price = {
            part: self.cleaned_data[field]
            for field, part in PRICE_FIELDS.items()
            if self.cleaned_data[field] is not None
        }
        set_product_setting(product.pk, "PRICE_REFERENCE", price or None)
        for field, key in LIST_FIELDS.items():
            set_product_setting(product.pk, key, _lines(self.cleaned_data[field]) or None)


def _validate_value(cleaned: dict[str, Any] | None, form: forms.BaseForm) -> dict[str, Any] | None:
    """Форма значения по ключу (`SETTING_PARSERS`) — ошибка у поля «значение»."""
    if cleaned is None:
        return None
    key, value = cleaned.get("key"), cleaned.get("value")
    if key and value is not None:
        try:
            validate_setting(key, value)
        except ValueError as error:
            form.add_error("value", str(error))
    return cleaned


class SettingValueField(forms.JSONField):
    """Значение настройки: у новой строки поле пустое, а не «null»."""

    def prepare_value(self, value: Any) -> Any:
        if value is None:
            return ""
        return super().prepare_value(value)


class OtherSettingForm(forms.ModelForm):  # type: ignore[type-arg]
    value = SettingValueField(
        label="значение", widget=forms.Textarea(attrs={"rows": 2, "cols": 40})
    )
    key = forms.ChoiceField(
        label="настройка",
        choices=[("", "—")]
        + [
            (key, f"{key} — {text}")
            for key, text in SETTING_KEYS.items()
            if key not in PRODUCT_KEYS
        ],
    )

    class Meta:
        model = DomainSetting
        fields = ("key", "value", "description")
        widgets = {  # noqa: RUF012 — так Django описывает виджеты формы
            "description": forms.Textarea(attrs={"rows": 2, "cols": 30}),
        }

    def clean_key(self) -> str:
        # Продукт в форму строки не входит, и Django не сверит уникальность
        # с базой: строку с тем же ключом могли добавить в другой вкладке.
        key: str = self.cleaned_data["key"]
        product_id = self.instance.product_id
        if product_id is not None:
            taken = DomainSetting.objects.filter(key=key, product_id=product_id).exclude(
                pk=self.instance.pk
            )
            if taken.exists():
                raise forms.ValidationError("Эта настройка у продукта уже переопределена.")
        return key

    def clean(self) -> dict[str, Any] | None:
        return _validate_value(super().clean(), self)


class ProductOtherSettingsInline(TabularInline):
    """Любая другая настройка, переопределённая для продукта (ADR-035).

    Основные настройки продукта сюда не попадают — у них свои поля выше.
    Удалить строку — продукт вернётся к общему значению.
    """

    model = DomainSetting
    form = OtherSettingForm
    fields = ("key", "value", "general_value", "description")
    readonly_fields = ("general_value",)
    extra = 0
    # «Добавить еще один Параметр продукта» — кнопка под таблицей.
    verbose_name = "параметр продукта"
    verbose_name_plural = "Другие настройки продукта — переопределяют общие"

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[DomainSetting]:
        return super().get_queryset(request).exclude(key__in=PRODUCT_KEYS)

    @admin.display(description="общее значение")
    def general_value(self, obj: DomainSetting) -> str:
        if not obj.key:
            return "—"
        value = get_setting(obj.key, None)
        return "нет" if value is None else _show(value)


class GeneralSettingForm(forms.ModelForm):  # type: ignore[type-arg]
    value = SettingValueField(label="значение")
    key = forms.ChoiceField(
        label="настройка",
        choices=[(key, f"{key} — {text}") for key, text in SETTING_KEYS.items()],
    )

    class Meta:
        model = DomainSetting
        fields = ("key", "value", "description")

    def clean_key(self) -> str:
        # Продукта в форме нет, поэтому Django не проверит уникальность сам.
        key: str = self.cleaned_data["key"]
        taken = DomainSetting.objects.filter(key=key, product__isnull=True).exclude(
            pk=self.instance.pk
        )
        if taken.exists():
            raise forms.ValidationError("Общее значение этой настройки уже есть.")
        return key

    def clean(self) -> dict[str, Any] | None:
        return _validate_value(super().clean(), self)


@admin.register(DomainSetting)
class DomainSettingAdmin(NoDeleteAdmin):
    """Раздел «Настройки»: общие значения для всех продуктов.

    Локальные значения здесь не показываются — их правят на странице продукта.
    """

    panel = True
    form = GeneralSettingForm
    list_display = ("key", "value_text", "description", "updated_at")
    search_fields = ("key", "description")
    readonly_fields = ("updated_at",)

    def get_queryset(self, request: HttpRequest) -> models.QuerySet[DomainSetting]:
        return super().get_queryset(request).filter(product__isnull=True)

    @admin.display(description="значение")
    def value_text(self, obj: DomainSetting) -> str:
        return _show(obj.value)
