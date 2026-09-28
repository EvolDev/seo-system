"""Базовые классы админки для всех доменных приложений.

Удаление отключено везде: ничего не удаляем физически (ADR-008).
Инфраструктура для моделей всех приложений, поэтому живёт в `config/`,
как `config/db.py`. Тема Admin Interface — оформление поверх штатной
админки (ADR-038), поэтому и классы здесь штатные, Django.
"""

from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.utils.text import capfirst

# В заглушках типов (django-stubs) классы админки параметризуются моделью:
# ModelAdmin[Site]. Сам Django так писать не даёт — `admin.ModelAdmin[Any]`
# упадёт при запуске. TYPE_CHECKING истинно только для mypy: он видит
# параметризованный класс, а Python при запуске — обычный.
if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[Any]
    _TabularInline = admin.TabularInline[Any, Any]
    _StackedInline = admin.StackedInline[Any, Any]
else:
    _ModelAdmin = admin.ModelAdmin
    _TabularInline = admin.TabularInline
    _StackedInline = admin.StackedInline

# В schema.sql все строки — text, и Django рисует для каждой многострочное
# поле ввода. Многострочными оставляем только поля, где пишут абзац;
# название, домен, адрес, анкор — однострочные (E9-08).
LONG_TEXT_FIELDS = frozenset(
    {
        "body",
        "comment",
        "context_sentence",
        "description",
        "error",
        "notes",
        "reject_reason",
        "summary",
    }
)


# Аннотации `models.Field[...]` — в кавычках: при запуске Field не параметризуется,
# это видит только mypy.
def _short_text_input(db_field: "models.Field[Any, Any]", kwargs: dict[str, Any]) -> None:
    """Однострочное поле для короткого text; свой виджет формы не трогаем."""
    if (
        isinstance(db_field, models.TextField)
        and db_field.name not in LONG_TEXT_FIELDS
        and "widget" not in kwargs
    ):
        kwargs["widget"] = forms.TextInput(attrs={"class": "vTextField"})


class ModelAdmin(_ModelAdmin):
    def formfield_for_dbfield(
        self, db_field: "models.Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> forms.Field | None:
        _short_text_input(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class TabularInline(_TabularInline):
    def formfield_for_dbfield(
        self, db_field: "models.Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> forms.Field | None:
        _short_text_input(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class StackedInline(_StackedInline):
    def formfield_for_dbfield(
        self, db_field: "models.Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> forms.Field | None:
        _short_text_input(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class NoDeleteAdmin(ModelAdmin):
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    # Заголовки страниц. Штатные — «Выберите площадка для изменения», «Изменить
    # площадка»: название модели в единственном числе, в именительном падеже.
    # У списка — название во множественном («Площадки»), у записи — название
    # модели над ней (сама запись — подзаголовком, как штатно).
    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        title = capfirst(str(self.model._meta.verbose_name_plural))
        return super().changelist_view(request, {"title": title, **(extra_context or {})})

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        title = capfirst(str(self.model._meta.verbose_name))
        return super().change_view(
            request, object_id, form_url, {"title": title, **(extra_context or {})}
        )

    def add_view(
        self, request: HttpRequest, form_url: str = "", extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        title = f"{capfirst(str(self.model._meta.verbose_name))} — новая запись"
        return super().add_view(request, form_url, {"title": title, **(extra_context or {})})


class SnapshotAdmin(NoDeleteAdmin):
    """Снапшоты (ADR-008, уточнение 27.09.2026).

    Новый замер — «Сохранить как новый объект»: форма открывается с
    данными последнего снапшота, сохранение создаёт новую строку. Править
    можно только последний снапшот, более старые — только просмотр. Дата
    замера в форме не редактируется: у нового снапшота она своя.
    """

    save_as = True
    # Колонки, внутри которых снапшот «последний»: у метрик — площадка
    # (`site_id`), у аудита — площадка и продукт. Задаёт наследник.
    snapshot_key: tuple[str, ...]
    time_field = "checked_at"

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return (self.time_field,)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if obj is not None and not self.is_latest(obj):
            return False
        return super().has_change_permission(request, obj)

    def is_latest(self, obj: models.Model) -> bool:
        key = {column: getattr(obj, column) for column in self.snapshot_key}
        latest = (
            type(obj)
            ._default_manager.filter(**key)
            .order_by(f"-{self.time_field}", "-pk")
            .values_list("pk", flat=True)
            .first()
        )
        return bool(latest == obj.pk)
