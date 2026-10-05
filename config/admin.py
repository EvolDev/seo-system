"""Базовые классы админки для всех доменных приложений.

Удалить можно любую запись (ADR-060, вместо «ничего не удаляем» ADR-008):
подтверждение показывает, что уйдёт вместе с ней и где очистится ссылка
(`config/deletion.py`). Инфраструктура для моделей всех приложений, поэтому живёт в `config/`,
как `config/db.py`. Тема Admin Interface — оформление поверх штатной
админки (ADR-038), поэтому и классы здесь штатные, Django.

Панель записи (E9-11, ADR-048): у админки с `panel = True` запись из списка
открывается панелью справа поверх списка (`seo/panel.js`). Панель просит
форму с заголовком `X-Seo-Partial` — страница та же, но в шапке формы —
шапка панели, внизу — закреплённые «Сохранить» и «Отмена»
(`config/templates/admin/change_form.html`). Записали — ответ JSON с подписью
для сообщения, а не переход на список; ошибка в форме — снова форма, с
подсказками (код 200, как у штатной админки: ответ 400 браузер пишет в
консоль ошибкой, хотя ошибки нет).
"""

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.views.main import PAGE_VAR, ChangeList
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.text import capfirst

from config import deletion

# В заглушках типов (django-stubs) классы админки параметризуются моделью:
# ModelAdmin[Site]. Сам Django так писать не даёт — `admin.ModelAdmin[Any]`
# упадёт при запуске. TYPE_CHECKING истинно только для mypy: он видит
# параметризованный класс, а Python при запуске — обычный.
if TYPE_CHECKING:
    # Словарь пункта фильтра описан только в заглушках django-stubs.
    from django.contrib.admin.filters import _ListFilterChoices

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
    """Однострочное поле для короткого text; свой виджет формы не трогаем.

    Поле со списком значений (статусы — перечисления Postgres, в Django это
    тоже TextField) остаётся выпадающим списком: с полем ввода статус
    приходилось набирать кодом, и форма его не принимала (E9-09).
    """
    if (
        isinstance(db_field, models.TextField)
        and not db_field.choices
        and db_field.name not in LONG_TEXT_FIELDS
        and "widget" not in kwargs
    ):
        kwargs["widget"] = forms.TextInput(attrs={"class": "vTextField"})


# Сколько записей назвать по имени на странице подтверждения удаления.
DELETE_NAMES = 30

# Панель записи и окна просят у сервера только содержимое — этим заголовком.
PARTIAL_HEADER = "X-Seo-Partial"
# Строка одной записи (seo/panel.js): запись в панели сохранили — строке списка
# нужны свежие ячейки, и быстро. Список с этим параметром отдаёт только её, мимо
# фильтров и поиска, с теми же колонками, а заголовком ROW_MATCH_HEADER — подходит
# ли она ещё под фильтры (нет — строка остаётся блёклой).
ROW_PARAM = "_seo_row"
ROW_MATCH_HEADER = "X-Seo-Row-Match"

# Сколько строк на странице списка — выбором человека (просьба 05.10.2026).
PER_PAGE_PARAM = "per_page"
PER_PAGE_CHOICES = (50, 100, 250, 500)
_ROW_ATTR = "seo_row"


class MultiChoiceFilter(admin.SimpleListFilter):
    """Фильтр с галочками: можно отметить несколько значений сразу.

    Штатный фильтр админки — один выбор на поле. Здесь отмеченное живёт в
    адресе через запятую (`?seller=3,7`), каждая галочка — ссылка, которая
    добавляет или снимает своё значение (просьба пользователя 05.10.2026).
    Наследник пишет `lookups()` и `queryset()`, значения берёт из `values()`.
    """

    template = "admin/seo_multiselect_filter.html"
    all_label = "Все"
    # «Все» — переключатель: отмечены все (параметра нет) → не отмечен никто.
    # Пустому выбору нужен свой след в адресе, иначе он неотличим от «все».
    none_token = "-"

    def chosen(self) -> list[str]:
        """Что стоит в адресе: параметр повторяется — `?seller=3&seller=7`.

        Через запятую не пишем: значением бывает имя файла, а в нём запятая
        («Clideo, Размещения Clideo через барыг.csv»).
        """
        name = self.parameter_name or ""
        return [value for value in self.request.GET.getlist(name) if value]

    def values(self) -> list[str]:
        """Отмеченные значения."""
        found = self.chosen()
        return [] if found == [self.none_token] else found

    def is_empty(self) -> bool:
        """Снято всё: список пуст, пока не отметят кого-нибудь."""
        return self.chosen() == [self.none_token]

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:
        if self.is_empty():
            return queryset.none()
        values = self.values()
        return self.narrow(queryset, values) if values else queryset

    def narrow(self, queryset: Any, values: list[str]) -> Any:
        """Отбор по отмеченным значениям — его пишет наследник."""
        raise NotImplementedError

    def choices(self, changelist: Any) -> "Iterator[_ListFilterChoices]":
        """Пункты меню. Ничего не отмечено — значит показаны все, и галочки стоят у всех.

        Так из полного списка убирают лишнего одним щелчком, а не отмечают
        сорок пять нужных (просьба пользователя 05.10.2026).
        """
        chosen = self.values()
        everything = not chosen and not self.is_empty()
        yield {
            "selected": everything,
            # Отмечены все — «Все» снимает отметки; иначе возвращает все.
            "query_string": (
                changelist.get_query_string({self.parameter_name: self.none_token})
                if everything
                else changelist.get_query_string(remove=[self.parameter_name])
            ),
            "display": self.all_label,
        }
        for value, label in self.lookup_choices:
            text = str(value)
            picked = everything or text in chosen
            if everything:
                rest = [str(other) for other, _ in self.lookup_choices if str(other) != text]
            elif picked:
                rest = [v for v in chosen if v != text]
            else:
                rest = [*chosen, text]
            yield {
                "selected": picked,
                "query_string": (
                    changelist.get_query_string({self.parameter_name: rest})
                    if rest
                    else changelist.get_query_string(remove=[self.parameter_name])
                ),
                "display": label,
            }


class PerPageChangeList(ChangeList):
    """Список с выбором размера страницы: `?per_page=50…500` рядом с пагинатором.

    Админка берёт размер страницы из `list_per_page` админки — один на всех.
    Здесь человек выбирает его сам, выбор живёт в адресе (значит, попадает в
    «Мои фильтры» и в ссылку, которой можно поделиться). Чужой параметр админка
    приняла бы за отбор по полю, поэтому он убирается из параметров фильтров.
    """

    def get_filters_params(self, params: Any = None) -> Any:
        found = super().get_filters_params(params)
        found.pop(PER_PAGE_PARAM, None)
        return found

    def get_results(self, request: HttpRequest) -> None:
        chosen = request.GET.get(PER_PAGE_PARAM)
        if chosen and chosen.isdigit() and int(chosen) in PER_PAGE_CHOICES:
            self.list_per_page = int(chosen)
        super().get_results(request)

    def short_pages(self) -> list[dict[str, Any]]:
        """Короткий пагинатор для строки действий: 1 · 2 · 3 … последняя.

        Нужен наверху списка, чтобы не прокручивать таблицу до низа ради
        перехода (просьба пользователя 05.10.2026). Текущая страница в наборе
        всегда: иначе с десятой страницы непонятно, где находишься.
        """
        total = self.paginator.num_pages
        if total < 2:
            return []
        numbers = sorted({1, 2, 3, self.page_num, total} & set(range(1, total + 1)))
        pages: list[dict[str, Any]] = []
        previous = 0
        for number in numbers:
            if number - previous > 1:
                pages.append({"gap": True})
            pages.append(
                {
                    "gap": False,
                    "number": number,
                    "current": number == self.page_num,
                    "url": self.get_query_string({PAGE_VAR: number}),
                }
            )
            previous = number
        return pages

    def per_page_choices(self) -> list[dict[str, Any]]:
        """Пункты «по 50 / 100 / …»: смена размера возвращает на первую страницу."""
        return [
            {
                "size": size,
                "selected": size == self.list_per_page,
                # Номер страницы убираем совсем: `p=0` для Django — несуществующая
                # страница, и список сбрасывает вместе с ним все фильтры.
                "url": self.get_query_string({PER_PAGE_PARAM: size}, [PAGE_VAR]),
            }
            for size in PER_PAGE_CHOICES
        ]


def _row_changelist(base: Any, row: int) -> Any:
    """Список одной записи на основе класса списка админки (свои колонки и итоги).

    Класс списка у каждой админки свой (ChangeList и наследники), поэтому
    подкласс строится на лету, а типы — Any.
    """

    class RowChangeList(base):  # type: ignore[misc]
        seo_row_matches: bool | None = None

        def get_queryset(self, request: HttpRequest, exclude_parameters: Any = None) -> Any:
            # Отбор списка — только чтобы узнать, подходит ли запись под фильтры.
            filtered = super().get_queryset(request, exclude_parameters)
            self.seo_row_matches = filtered.filter(pk=row).exists()
            # getattr: тип атрибута у класса на лету mypy не выводит.
            root: Any = getattr(self, "root_queryset")  # noqa: B009
            one = self.apply_select_related(root.filter(pk=row).order_by("pk"))
            # «Всего N» список считает по root_queryset — пусть считает одну строку,
            # а не всю таблицу: ответ нужен быстрый.
            self.root_queryset = one
            return one

    return RowChangeList


def is_partial(request: HttpRequest) -> bool:
    return request.headers.get(PARTIAL_HEADER) == "1"


def _has_product(model: type[models.Model]) -> bool:
    """Есть ли у записи связь «продукт» — с продуктом из `products`."""
    try:
        field = model._meta.get_field("product")
    except FieldDoesNotExist:
        return False
    related = field.related_model
    return isinstance(related, type) and related._meta.db_table == "products"


class ModelAdmin(_ModelAdmin):
    # Запись из списка открывается панелью справа (E9-11). Включается у
    # рабочих списков и справочников; у снимков — нет: новый замер там —
    # «Сохранить как новый объект», в панели такой кнопки нет.
    panel = False

    def formfield_for_dbfield(
        self, db_field: "models.Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> forms.Field | None:
        _short_text_input(db_field, kwargs)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, Any]:
        """Новая запись с полем «продукт» — сразу с рабочим продуктом (E9-12, ADR-057)."""
        initial: dict[str, Any] = super().get_changeform_initial_data(request)
        if "product" not in initial and _has_product(self.model):
            from apps.workspace.products import working_product_id

            working = working_product_id(request)
            if working is not None:
                initial["product"] = working
        return initial

    # ---------- Панель записи ----------

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        # Параметр строки — не фильтр: убираем его до списка, иначе Django
        # принял бы его за условие отбора и сбросил фильтры с ?e=1.
        row = request.GET.get(ROW_PARAM)
        if row is None:
            return super().changelist_view(request, extra_context)
        query = request.GET.copy()
        del query[ROW_PARAM]
        query._mutable = False
        request.GET = query  # type: ignore[assignment]
        setattr(request, _ROW_ATTR, int(row) if row.isdigit() else 0)
        response = super().changelist_view(request, extra_context)
        changelist = (getattr(response, "context_data", None) or {}).get("cl")
        matches = getattr(changelist, "seo_row_matches", None)
        if matches is not None:
            response[ROW_MATCH_HEADER] = "1" if matches else "0"
        return response

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        return PerPageChangeList

    def get_changelist_instance(self, request: HttpRequest) -> Any:
        row = getattr(request, _ROW_ATTR, None)
        if row is None:
            return super().get_changelist_instance(request)
        # Как штатный ModelAdmin.get_changelist_instance (Django 5.2), но класс
        # списка — одной записи: та же выборка админки (аннотации колонок), без
        # фильтров и поиска, без подсчёта всей таблицы.
        list_display = self.get_list_display(request)
        list_display_links = self.get_list_display_links(request, list_display)
        if self.get_actions(request):
            list_display = ["action_checkbox", *list_display]
        changelist_class = _row_changelist(self.get_changelist(request), row)
        return changelist_class(
            request,
            self.model,
            list_display,
            list_display_links,
            self.get_list_filter(request),
            self.date_hierarchy,
            self.get_search_fields(request),
            self.get_list_select_related(request),
            self.list_per_page,
            self.list_max_show_all,
            self.list_editable,
            self,
            self.get_sortable_by(request),
            self.search_help_text,
        )

    def in_panel(self, request: HttpRequest) -> bool:
        return self.panel and is_partial(request)

    def panel_title(self, obj: Any) -> str:
        """Заголовок панели — запись; у размещения, например, «площадка · продукт»."""
        return str(obj)

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: Any = None,
    ) -> HttpResponse:
        panel = self.in_panel(request)
        context["seo_panel"] = panel
        if panel:
            name = capfirst(str(self.model._meta.verbose_name))
            context.update(
                {
                    "panel_title": f"{name} — новая запись" if add else self.panel_title(obj),
                    "panel_sub": "" if add else name,
                    # Тот же адрес без заголовка — полная страница формы.
                    "panel_full_url": request.get_full_path(),
                    "panel_can_save": self.has_add_permission(request)
                    if add
                    else self.has_change_permission(request, obj),
                }
            )
        return super().render_change_form(request, context, add, change, form_url, obj)

    def response_add(
        self, request: HttpRequest, obj: Any, post_url_continue: str | None = None
    ) -> HttpResponse:
        if self.in_panel(request):
            return self._panel_saved(request, obj, "добавлено")
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request: HttpRequest, obj: Any) -> HttpResponse:
        if self.in_panel(request):
            return self._panel_saved(request, obj, "сохранено")
        return super().response_change(request, obj)

    def _panel_saved(self, request: HttpRequest, obj: Any, done: str) -> JsonResponse:
        """Записано из панели: подпись для сообщения вместо перехода на список.

        Сообщения, которые положил код записи (`message_user`), — в ответ:
        иначе они всплыли бы на следующем экране, к которому не относятся.
        """
        name = capfirst(str(self.model._meta.verbose_name))
        notes = [str(note) for note in messages.get_messages(request)]
        return JsonResponse(
            {
                "saved": True,
                "pk": obj.pk,
                "message": f"{name} «{self.panel_title(obj)}» — {done}.",
                "notes": notes,
            }
        )


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


class RecordAdmin(ModelAdmin):
    # ---------- Удаление (ADR-060) ----------
    # Штатные страница подтверждения и действие «Удалить выбранные» Django
    # спрашивают у админки, что удалится (`get_deleted_objects`), и потом
    # удаляют (`delete_model`, `delete_queryset`). Здесь оба шага идут через
    # `config.deletion`: вместе с записью — всё, что без неё не живёт.

    def delete_roots(self, objs: Iterable[Any]) -> dict[deletion.Model, list[int]]:
        """Что удаляем на самом деле. У списков поверх представлений — свою запись."""
        return {self.model: [obj.pk for obj in objs]}

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Строку представления удалить нельзя: удаляется запись, на которой оно
        # стоит, — это говорит `delete_roots` списка («Площадки» → площадка).
        if not self.model._meta.managed and type(self).delete_roots is RecordAdmin.delete_roots:
            return False
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(
        self, objs: Any, request: HttpRequest
    ) -> tuple[list[Any], dict[str, int], set[str], list[str]]:
        objects = list(objs)
        plan = deletion.collect(self.delete_roots(objects))
        names: list[Any] = [str(obj) for obj in objects[:DELETE_NAMES]]
        if len(objects) > DELETE_NAMES:
            names.append(f"…и ещё {len(objects) - DELETE_NAMES}")
        counts = {line.text: line.count for line in plan.lines()}
        return names, counts, set(), []

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        deletion.delete(self.delete_roots([obj]))

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        deletion.delete(self.delete_roots(queryset))

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


class SnapshotAdmin(RecordAdmin):
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
