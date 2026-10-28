"""Пользователи: карточка панелью, роль кнопками, особые права по разделам (ADR-078).

Штатная страница Django — двести прав одной кучей по-английски («Can add log
entry»), два списка со стрелками, пароль — отдельной страницей. Здесь:
- **Человек** — имя (его видно в окошке блокировки, в «Сотруднике», в заметках),
  фамилия, логин, почта;
- **Вход в систему** — «Может входить» и новый пароль дважды прямо в форме;
- **Роль** — «Администратор» (полный доступ), «Линкбилдер», «Только просмотр»,
  «Без роли» (`apps/workspace/roles.py`);
- **Особые права** — исключения к роли: таблица по разделам меню, права роли в ней
  отмечены и серые (`admin/workspace/permission_table.html`, `seo/user-perms.js`).

Себе снять вход или полный доступ нельзя — останешься без доступа. Группы в меню не
показываются: группы ролей ведёт код, свои группы не нужны.
"""

import json
from typing import TYPE_CHECKING, Any, ClassVar

from django import forms
from django.contrib import admin
from django.contrib.auth import admin as auth_admin
from django.contrib.auth import password_validation
from django.contrib.auth.models import Group, Permission, User
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html_join
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import capfirst
from django.views.decorators.debug import sensitive_post_parameters

from apps.workspace import roles
from config.admin import ModelAdmin
from config.admin_site import HIDDEN, MENU
from config.assets import Css, Js
from config.forms import ChoiceButtons

# Заглушки типов параметризуют админку моделью, сам Django — нет (как в config/admin.py).
if TYPE_CHECKING:
    _UserAdmin = auth_admin.UserAdmin[User]
else:
    _UserAdmin = auth_admin.UserAdmin

ACTIONS = (("view", "Смотреть"), ("add", "Добавлять"), ("change", "Менять"), ("delete", "Удалять"))
# Данные под экранами — таблицы линкбилдинга, которых нет в меню: ссылки, заметки,
# цены, история статусов. Остальное — «Система»: журнал, сессии, права, темы.
_DOMAIN_APPS = ("sites", "placements", "keywords", "content", "observability", "workspace")


def _sections(chosen: set[str]) -> list[dict[str, Any]]:
    """Права по разделам меню: строка — таблица, колонки — смотреть, добавлять, менять, удалять."""
    by_model: dict[tuple[str, str], dict[str, Permission]] = {}
    labels: dict[tuple[str, str], str] = {}
    for permission in Permission.objects.select_related("content_type"):
        content_type = permission.content_type
        key = (content_type.app_label, content_type.model)
        by_model.setdefault(key, {})[permission.codename.split("_", 1)[0]] = permission
        if key not in labels:
            model = content_type.model_class()
            name = model._meta.verbose_name_plural if model is not None else content_type.model
            labels[key] = capfirst(str(name))

    def row(key: tuple[str, str]) -> dict[str, Any]:
        found = by_model[key]
        cells = []
        for code, title in ACTIONS:
            permission = found.get(code)
            cells.append(
                {
                    "id": str(permission.pk),
                    "action": code,
                    "title": title,
                    "checked": str(permission.pk) in chosen,
                }
                if permission is not None
                else None
            )
        return {"label": labels[key], "search": labels[key].lower(), "cells": cells}

    sections = []
    placed: set[tuple[str, str]] = set()
    for title, _, keys in MENU:
        present = [key for key in keys if key in by_model]
        placed.update(present)
        if present:
            sections.append({"title": title, "rows": [row(key) for key in present], "open": True})
    rest = sorted((key for key in by_model if key not in placed), key=lambda key: labels[key])
    data = [key for key in rest if key[0] in _DOMAIN_APPS and key not in HIDDEN]
    system = [key for key in rest if key not in data]
    sections.append(
        {
            "title": "Данные под экранами",
            "hint": "Ссылки, заметки, цены, история статусов — то, что меняется на экранах выше.",
            "rows": [row(key) for key in data],
            "open": False,
        }
    )
    sections.append(
        {
            "title": "Система",
            "hint": "Журнал действий, сессии, права, темы оформления.",
            "rows": [row(key) for key in system],
            "open": False,
        }
    )
    return sections


class PermissionTable(forms.Widget):
    """Особые права — таблицей по разделам меню (admin/workspace/permission_table.html)."""

    template_name = "admin/workspace/permission_table.html"
    allow_multiple_selected = True

    class Media:
        js = (Js("seo/user-perms.js"),)
        css: ClassVar[dict[str, tuple[Css, ...]]] = {"all": (Css("seo/user-perms.css"),)}

    def value_from_datadict(self, data: Any, files: Any, name: str) -> Any:
        return data.getlist(name) if hasattr(data, "getlist") else data.get(name)

    def render(
        self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None
    ) -> SafeString:
        # Шаблон — в config/templates: штатный отрисовщик виджетов форм туда не смотрит.
        return mark_safe(render_to_string(self.template_name, self.get_context(name, value, attrs)))

    def get_context(self, name: str, value: Any, attrs: dict[str, Any] | None) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        chosen = {str(getattr(item, "pk", item)) for item in (value or [])}
        context["sections"] = _sections(chosen)
        context["actions"] = ACTIONS
        # Права ролей — отметить в таблице серыми (seo/user-perms.js).
        context["role_perms"] = json.dumps(
            {role: [p.pk for p in roles.permissions(role)] for role in roles.GROUPS}
        )
        return context


class UserForm(forms.ModelForm):  # type: ignore[type-arg]
    """Карточка пользователя: человек, вход, роль, особые права.

    `editor_id` — кто правит (ставит админка): себе снять вход или полный доступ
    нельзя.
    """

    editor_id: int | None = None

    can_login = forms.BooleanField(
        label="Может входить в систему",
        required=False,
        help_text="Без отметки человек остаётся сотрудником в списках, но войти не может.",
    )
    password1 = forms.CharField(
        label="Новый пароль",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Пароль ещё раз",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    role = forms.ChoiceField(
        label="Роль", choices=roles.CHOICES, required=False, widget=ChoiceButtons
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "username", "email", "user_permissions")
        labels: ClassVar[dict[str, str]] = {
            "first_name": "Имя",
            "last_name": "Фамилия",
            "username": "Логин",
            "email": "Почта",
            "user_permissions": "Особые права",
        }
        help_texts: ClassVar[dict[str, str]] = {
            "first_name": "Так человека видно в окошке блокировки, в «Сотруднике» и в заметках.",
            "username": "Для входа: буквы, цифры и @ . + - _.",
            "user_permissions": "",
        }
        widgets: ClassVar[dict[str, Any]] = {"user_permissions": PermissionTable}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        user = self.instance
        new = user.pk is None
        self.fields["can_login"].initial = True if new else user.is_staff and user.is_active
        self.fields["role"].initial = roles.LINKBUILDER if new else roles.role_of(user)
        # Под кнопками — что даёт выбранная роль (seo/user-perms.js прячет остальные).
        self.fields["role"].help_text = format_html_join(
            "",
            '<span class="seo-role-hint" data-role-hint="{}"><b>{}</b> — {}</span>',
            ((code, name, roles.HINTS[code]) for code, name in roles.CHOICES),
        )
        if new:
            self.fields["password1"].help_text = "Человек войдёт с этим паролем."
        elif user.has_usable_password():
            self.fields["password1"].help_text = "Пароль задан. Оставьте пустым — не изменится."
        else:
            self.fields["password1"].help_text = "Пароля нет — войти нельзя, пока не зададите."
        self.fields["user_permissions"].required = False

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        first, second = data.get("password1") or "", data.get("password2") or ""
        if first != second:
            self.add_error("password2", "Пароли не совпадают.")
        elif first:
            try:
                password_validation.validate_password(first, self.instance)
            except forms.ValidationError as error:
                self.add_error("password1", error)
        has_password = bool(first) or (
            self.instance.pk is not None and self.instance.has_usable_password()
        )
        if data.get("can_login") and not has_password:
            self.add_error("password1", "Задайте пароль — без него войти нельзя.")
        if self.instance.pk is not None and self.instance.pk == self.editor_id:
            if not data.get("can_login"):
                self.add_error("can_login", "Себе снять вход нельзя — останетесь без доступа.")
            if self.instance.is_superuser and data.get("role") != roles.ADMIN:
                self.add_error(
                    "role",
                    "Себе снять полный доступ нельзя — пусть это сделает другой администратор.",
                )
        return data

    def save(self, commit: bool = True) -> User:
        user: User = super().save(commit=False)
        if self.cleaned_data.get("can_login"):
            user.is_staff = user.is_active = True
        else:
            user.is_staff = False
        if self.cleaned_data.get("password1"):
            user.set_password(self.cleaned_data["password1"])
        elif user.pk is None:
            user.set_unusable_password()
        if commit:
            user.save()
            self.save_m2m()
        return user


class LoginFilter(admin.SimpleListFilter):
    title = "вход"
    parameter_name = "login"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Может входить"), ("no", "Без входа")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[User]) -> QuerySet[User]:
        can = Q(is_staff=True, is_active=True)
        if self.value() == "yes":
            return queryset.filter(can)
        if self.value() == "no":
            return queryset.exclude(can)
        return queryset


class RoleFilter(admin.SimpleListFilter):
    title = "роль"
    parameter_name = "role"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [(code or "none", name) for code, name in roles.CHOICES]

    def queryset(self, request: HttpRequest, queryset: QuerySet[User]) -> QuerySet[User]:
        value = self.value()
        if value == roles.ADMIN:
            return queryset.filter(is_superuser=True)
        if value in roles.GROUPS:
            return queryset.filter(is_superuser=False, groups__name=roles.GROUPS[value])
        if value == "none":
            return queryset.filter(is_superuser=False).exclude(
                groups__name__in=roles.GROUPS.values()
            )
        return queryset


admin.site.unregister(User)
# Группы ролей ведёт код (roles.sync) — экрана групп нет.
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(ModelAdmin, _UserAdmin):
    panel = True
    form = add_form = UserForm
    add_form_template = None
    filter_horizontal = ()
    list_display = ("name_cell", "login_name", "login_cell", "role_cell", "last_login_cell")
    list_display_links = ("name_cell", "login_name")
    list_filter = (LoginFilter, RoleFilter)
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("first_name", "username")
    readonly_fields = ("last_login_text", "joined_text")
    fieldsets = (
        ("Человек", {"fields": ("first_name", "last_name", "username", "email")}),
        ("Вход в систему", {"fields": ("can_login", "password1", "password2")}),
        ("Роль", {"fields": ("role",)}),
        (
            "Особые права",
            {
                "fields": ("user_permissions",),
                "description": "Исключения к роли. Права роли отмечены серым — отдельно их"
                " давать не нужно.",
            },
        ),
        ("Служебное", {"fields": ("last_login_text", "joined_text")}),
    )
    add_fieldsets = fieldsets[:4]

    def get_queryset(self, request: HttpRequest) -> QuerySet[User]:
        queryset: QuerySet[User] = (
            super()
            .get_queryset(request)
            .prefetch_related("groups")
            .annotate(own_perms=Count("user_permissions", distinct=True))
        )
        return queryset

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> Any:
        form = super().get_form(request, obj, change=change, **kwargs)
        # Класс формы свой на каждый запрос — кто правит, знает только он.
        form.editor_id = request.user.pk  # type: ignore[attr-defined]
        return form

    # Заголовки страниц по-человечески: штатные — «Выберите пользователь для изменения».
    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> Any:
        extra = {"title": "Пользователи", "hide_copy_domains": True, **(extra_context or {})}
        return super().changelist_view(request, extra)

    def add_view(
        self, request: HttpRequest, form_url: str = "", extra_context: dict[str, Any] | None = None
    ) -> Any:
        extra = {"title": "Новый пользователь", **(extra_context or {})}
        return super().add_view(request, form_url, extra)

    # Новый пароль — в форме карточки: в отчёт об ошибке он не попадает.
    @method_decorator(sensitive_post_parameters("password1", "password2"))
    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        extra = {"title": "Пользователь", **(extra_context or {})}
        return super().change_view(request, object_id, form_url, extra)

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        roles.set_role(form.instance, form.cleaned_data.get("role") or roles.NONE)

    def panel_title(self, obj: Any) -> str:
        name: str = obj.get_full_name() or obj.get_username()
        return name

    def panel_add_title(self, request: HttpRequest) -> str:
        return "Новый пользователь"

    @admin.display(description="имя", ordering="first_name")
    def name_cell(self, obj: User) -> str:
        return obj.get_full_name() or "—"

    @admin.display(description="логин", ordering="username")
    def login_name(self, obj: User) -> str:
        return obj.get_username()

    @admin.display(description="может входить", boolean=True)
    def login_cell(self, obj: User) -> bool:
        return obj.is_staff and obj.is_active

    @admin.display(description="роль")
    def role_cell(self, obj: User) -> str:
        if obj.is_superuser:
            text = roles.label(roles.ADMIN)
        else:
            names = {group.name for group in obj.groups.all()}
            found = [code for code, name in roles.GROUPS.items() if name in names]
            text = roles.label(found[0]) if found else "—"
        own = getattr(obj, "own_perms", 0)
        if own and not obj.is_superuser:
            text += f" · особых прав: {own}"
        return text

    @admin.display(description="последний вход", ordering="last_login")
    def last_login_cell(self, obj: User) -> str:
        return _moment(obj.last_login)

    @admin.display(description="последний вход")
    def last_login_text(self, obj: User) -> str:
        return _moment(obj.last_login) or "входа ещё не было"

    @admin.display(description="заведён")
    def joined_text(self, obj: User) -> str:
        return _moment(obj.date_joined)


def _moment(moment: Any) -> str:
    return f"{timezone.localtime(moment):%d.%m.%Y %H:%M}" if moment else ""
