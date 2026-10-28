"""Админка блока 2: размещения и ссылки в них.

Удаление отключено: размещение отменяется статусом «Отменено», ссылка
исправляется, но не удаляется.

Проверка индексации (E2-03): кнопка в карточке и действие в списке ставят
проверку в очередь, в карточке — история проверок. «Не проверять» убирает
размещение из проверок по расписанию.

Размещение открывается панелью справа поверх списка (E9-11): щелчок по
площадке или статусу. Форма — `apps/placements/forms.py`: статус кнопками,
поля группами, даты без времени, «Заплачено» в валюте.

Отчёт за месяц (E1-06): фильтр «опубликовано» по месяцам и кнопки «Выгрузить
в Excel» и «CSV» — лист «Размещения» таблицы и «Итого» (`export.py`).

Счета (E1-14, ADR-055): действие «Счёт на отмеченные» открывает форму нового
счёта с ними (`invoice_admin.py`), фильтр и колонка «счёт». «Заплачено»
размещения в счёте — из счёта: в форме оно текстом со ссылкой на счёт.

Повторные размещения (ADR-073): над формой — вкладки площадки «Площадка · №1 ·
№2 · + Ещё размещение» (`repeat.tabs`); новое размещение берёт предложение
продавца выбором «Через кого», сотрудник — тот, кто вошёл. Удаляют размещение
красной корзиной на его вкладке, по одному: «Удалить выбранные» в списке нет.
Размещение другого продукта, чем рабочий в шапке, — только просмотр.
"""

from collections.abc import Iterator, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, timedelta
from typing import Any, ClassVar
from uuid import uuid4

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.utils import display_for_value
from django.contrib.admin.views.main import ChangeList
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count, Exists, OuterRef, QuerySet, Subquery
from django.db.models.functions import TruncMonth
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.keywords import anchor_views, regions
from apps.keywords.models import AnchorList
from apps.observability.models import Check, CheckStatus, Performer, TaskRun, TaskStatus
from apps.placements import export as placement_export
from apps.placements import (
    export_designer_view,
    invoices,
    other_products,
    repeat,
    status_chips,
)
from apps.placements.forms import PlacementForm, PlacementLinkForm, PlacementLinkFormSet
from apps.placements.indexation import CHECK_TYPE, ENTITY_TYPE, page_url
from apps.placements.models import (
    STATUS_STAMPS,
    InvoiceItem,
    InvoiceStatus,
    Placement,
    PlacementLink,
)
from apps.placements.tasks import check_indexation, check_url_indexation, url_result_key
from apps.sites import countries, rating, statuses
from apps.sites.display import domain_tools_html, rating_html
from apps.sites.languages import top_geo_of
from apps.sites.models import (
    Product,
    Seller,
    Site,
    SiteLatest,
    SiteOffer,
    SitePrice,
    StatusSource,
    WorkStatus,
)
from apps.sites.offers import describe, money, set_working_price
from apps.sites.status_history import placement_history
from apps.workspace.products import FrameProductFilter, working_product_id
from config import export
from config.admin import MultiChoiceFilter, PerPageChangeList, RecordAdmin, StackedInline
from config.assets import Css, Js
from config.changes import stamped
from config.export import month_name
from config.queue import MAX_ATTEMPTS
from config.run_id import bind_run_id, new_run_id

# Сколько последних проверок индексации показывать в карточке.
HISTORY_LIMIT = 20
QUEUED = "Проверка индексации поставлена в очередь — обновите страницу через несколько секунд."
# Строку запуска проверки ищем среди запусков за это время: кнопку жмут
# и смотрят на результат сразу, повторы идут минуты.
STATUS_LOOKBACK = timedelta(days=1)
# Больше размещений в одном счёте не бывает: отмечено больше — скорее ошибка.
INVOICE_MAX_PLACEMENTS = 100


def _in_invoice(status: str | None = None) -> Exists:
    """Размещение в неотменённом счёте; status — только в счёте с этим статусом."""
    items = InvoiceItem.objects.filter(placement=OuterRef("pk"))
    if status is None:
        items = items.exclude(invoice__status=InvoiceStatus.CANCELLED)
    else:
        items = items.filter(invoice__status=status)
    return Exists(items)


class InvoiceFilter(admin.SimpleListFilter):
    """Счёт размещения (E1-14): нет счёта, выставлен — ждёт оплаты, оплачен."""

    title = "счёт"
    parameter_name = "invoice"
    NONE = "none"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            (self.NONE, "нет счёта"),
            (InvoiceStatus.ISSUED, "выставлен, не оплачен"),
            (InvoiceStatus.PAID, "оплачен"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Placement]) -> Any:
        value = self.value()
        if value == self.NONE:
            return queryset.filter(~_in_invoice())
        if value in (InvoiceStatus.ISSUED, InvoiceStatus.PAID):
            return queryset.filter(_in_invoice(value))
        return queryset


class PublishedMonthFilter(admin.SimpleListFilter):
    """Месяц публикации — отчёт за месяц (E1-06). Месяц — по времени проекта.

    В списке — месяцы, в которые что-то опубликовано, свежие первыми, и
    «без даты».
    """

    title = "опубликовано"
    parameter_name = "month"
    NO_DATE = "none"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        # TruncMonth — первое число месяца по времени проекта, а не по UTC.
        months = (
            Placement.objects.filter(published_at__isnull=False)
            .annotate(month=TruncMonth("published_at"))
            .order_by("-month")
            .values_list("month", flat=True)
            .distinct()
        )
        choices = [(f"{month:%Y-%m}", month_name(month.date())) for month in months]
        return [*choices, (self.NO_DATE, "без даты")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Placement]) -> Any:
        value = self.value()
        if not value:
            return queryset
        if value == self.NO_DATE:
            return queryset.filter(published_at__isnull=True)
        start = parse_month(value)
        if start is None:
            return queryset.none()
        end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        return queryset.filter(
            published_at__gte=_local_midnight(start), published_at__lt=_local_midnight(end)
        )


def parse_month(value: str | None) -> date | None:
    """«2026-09» → 1 сентября 2026; не месяц — None."""
    try:
        return datetime.strptime(value or "", "%Y-%m").date()
    except ValueError:
        return None


def _local_midnight(day: date) -> datetime:
    return timezone.make_aware(datetime(day.year, day.month, day.day))


class PlacementLinkInline(StackedInline):
    """Ссылки размещения: задание правит человек, остальное — проверка страницы.

    Задание — анкор из списка анкоров продукта, текст на сайте, вид ссылки и
    куда ведёт (E3-05, ADR-059, ADR-069): поле анкора с поиском и окном «Новый
    анкор» (seo/anchors.js), адрес подставляется из анкора, ключ во фразе
    подсказывает сервер. Номер ссылки ставится сам, тест на извлечение
    в форме не показывается — до проверок статьи (E7-02).

    Что на странице (`rel`, позиция, живость, время пропажи), пишут
    краулер и проверка живости (E2-04, E2-05) — в форме только чтение:
    время пропажи пишется один раз.

    Ссылку убирают ⊗ в её заголовке, как новую (ADR-060): блок сворачивается
    в «уберётся при сохранении · Вернуть», удаляется по «Сохранить»
    (`admin/edit_inline/stacked_remove.html`, seo/inline-remove.css).
    """

    model = PlacementLink
    form = PlacementLinkForm
    formset = PlacementLinkFormSet
    template = "admin/edit_inline/stacked_remove.html"
    readonly_fields = (
        "is_alive",
        "rel",
        "char_offset",
        "char_offset_no_spaces",
        "context_sentence",
        "first_seen_at",
        "last_checked_at",
        "lost_at",
    )
    # По одному полю в строке: панель размещения — по ширине содержимого (E9-11).
    fieldsets = (
        (
            None,
            {"fields": ("keyword", "text", "kind", "target_url")},
        ),
        (
            "На странице — заполняет проверка",
            {
                "classes": ("collapse",),
                "fields": (
                    ("is_alive", "rel"),
                    ("char_offset", "char_offset_no_spaces"),
                    "context_sentence",
                    ("first_seen_at", "last_checked_at", "lost_at"),
                ),
            },
        ),
    )

    def get_extra(self, request: HttpRequest, obj: Any = None, **kwargs: Any) -> int:
        # У нового размещения сразу два слота: ссылок в статье одна-две.
        return 2 if obj is None else 0

    # Ссылки размещения другого продукта — только просмотр, как и само размещение.
    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return own_product(request, obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return own_product(request, obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return own_product(request, obj) and super().has_delete_permission(request, obj)


def own_product(request: HttpRequest, obj: Any = None) -> bool:
    """Размещение рабочего продукта из шапки — его можно менять (ADR-073).

    Чужой продукт — только просмотр: менять его — переключив продукт в шапке,
    чтобы не задеть случайно (просьба пользователя 18.10.2026).
    """
    return obj is None or obj.product_id == working_product_id(request)


def _initial_product(context: dict[str, Any]) -> int | None:
    """Продукт новой формы размещения — начальное значение поля (рабочий продукт)."""
    form = context.get("adminform")
    value = form.form.initial.get("product") if form is not None else None
    product = getattr(value, "pk", value)
    return int(product) if isinstance(product, int | str) and str(product).isdigit() else None


def _anchor_choice(
    context: dict[str, Any], obj: Placement | None, lists: Sequence[AnchorList]
) -> regions.Choice:
    """Список анкоров формы (ADR-075): выбранный до ошибки формы, сохранённый, угаданный.

    Новое размещение угадывается по площадке из адреса (вкладки площадки), без
    неё — основной список; сменят площадку или продукт — блок придёт заново.
    """
    form = context.get("adminform")
    data = form.form.data if form is not None else {}
    by_id = {str(item.pk): item for item in lists}
    if data.get("anchor_list") in by_id:
        return regions.Choice(by_id[data["anchor_list"]], "выбран вами")
    if obj is not None and obj.pk is not None:
        return regions.for_placement(obj, lists)
    value = form.form.initial.get("site") if form is not None else None
    site_id = getattr(value, "pk", value)
    site = (
        Site.all_objects.filter(pk=site_id).first()
        if isinstance(site_id, int | str) and str(site_id).isdigit()
        else None
    )
    if site is not None:
        return regions.choose(site, lists, top_geo=top_geo_of(site.pk))
    return regions.Choice(lists[0] if lists else None, "")


def _queue_checks(placement_ids: list[int]) -> list[str]:
    """Ставит ручные проверки индексации, возвращает id задач очереди.

    Нажатие кнопки — начало цепочки (ADR-025): у проверок свой run_id.
    """
    with bind_run_id(new_run_id()):
        return [
            check_indexation.delay(placement_id, manual=True).id for placement_id in placement_ids
        ]


# Группы формы размещения; ссылки статьи (инлайн) — между «Публикацией» и
# «Проверками»: Django рисует их после всех групп, поэтому группы после
# ссылок шаблон рисует отдельно (admin/placements/placement/change_form.html).
REQUEST = "Заявка"
ARTICLE = "Статья"
PUBLICATION = "Публикация"
CHECKS = "Проверки"
COMMENT = "Комментарий"
FROM_FILE = "Из файла размещений"
SERVICE = "Служебное"
AFTER_LINKS = (CHECKS, COMMENT, SERVICE)


class StatusActionForm(helpers.ActionForm):
    """Поле «статус» рядом с выбором действия — для «Поставить статус…»."""

    status = forms.ChoiceField(
        choices=[("", "статус…"), *WorkStatus.choices],
        required=False,
        label="статус",
    )


class PlacementChangeList(PerPageChangeList):
    """Список размещений: одна строка на площадку, фишки и другие продукты строк.

    Строка — площадка (ADR-073): из размещений, что прошли фильтры, — текущее
    (идущее, иначе последнее); колонки — его, остальные размещения — фишками в
    «статусе» и вкладками в панели. Одна строка на площадку — только у страницы:
    действие над отмеченными берёт отмеченные размещения как есть, «Выбрать все»
    — строки списка (`PlacementAdmin.response_action`). Выгрузка — не этот класс:
    она по каждому размещению.

    Одним запросом на страницу, как предложения цен в «Площадках»: колонки
    «другие продукты» и «статус» берут готовое.
    """

    def get_results(self, request: HttpRequest) -> None:
        # «N всего» считается по этому запросу: он должен быть в той же рамке,
        # что и строки, иначе счётчик говорит про оба продукта (E1-20), — и по
        # площадкам, как строки (ADR-073).
        working = working_product_id(request)
        framed = self.root_queryset.filter(product_id=working)
        self.root_queryset = repeat.one_per_site(framed)
        self.queryset = repeat.one_per_site(self.queryset)
        super().get_results(request)
        # Действие над отмеченными строит выборку заново от root_queryset: там
        # размещения как есть, а не по одному на площадку.
        self.root_queryset = framed
        found = other_products.by_site(row.site_id for row in self.result_list)
        cells = status_chips.cells(
            (row.site_id for row in self.result_list), working, status_chips.period_of(self)
        )
        # Цены строки — предложения размещения (ADR-073): номера пришли в выборке.
        offers = SitePrice.objects.in_bulk(
            {row.deal_offer_id for row in self.result_list if row.deal_offer_id is not None}
        )
        for row in self.result_list:
            row.page_products = found.get(row.site_id, [])
            row.page_cell = cells.get(row.site_id)
            row.page_offer = offers.get(row.deal_offer_id)


class PlacementRegionFilter(admin.SimpleListFilter):
    """Регион размещения (ADR-076): страна его списка анкоров — выбранного в блоке
    анкоров карточки или угаданного по ссылкам, языку и топ-региону площадки
    (`apps/keywords/regions.py`). По нему отбирают, например, бразильские
    размещения для отдельной выгрузки. «Не определён» — регион не известен:
    основной список взят по умолчанию; в выгрузке у таких «Region» пустой.

    Выбор не запоминается: забытый регион молча прятал бы размещения других стран.
    """

    title = "регион"
    parameter_name = "region"
    UNKNOWN = "unknown"
    # Флаги и поиск поверх выпадающего списка — как «Регион» в «Площадках».
    template = "admin/sites/region_filter.html"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        working = working_product_id(request)
        lists = AnchorList.of_product(working) if working is not None else []
        return [(item.country, item.region_name) for item in lists]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        if not value:
            return queryset
        # Список тянет цены площадки (`list_select_related`) — региону они не нужны.
        placements = list(
            queryset.select_related(None)
            .select_related("site")
            .only("pk", "product_id", "anchor_list_id", "site__domain", "site__language")
        )
        found = {
            pk: regions.known_region(choice)
            for pk, choice in regions.effective_choices(placements).items()
        }
        wanted = None if value == self.UNKNOWN else value
        return queryset.filter(pk__in=[pk for pk, code in found.items() if code == wanted])

    def choices(self, changelist: Any) -> Iterator[Any]:
        yield {
            "selected": not self.value(),
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": "Все регионы",
            "flag": countries.GLOBE,
            "search": "все регионы all",
        }
        for code, title in self.lookup_choices:
            yield {
                "selected": self.value() == code,
                "query_string": changelist.get_query_string({self.parameter_name: code}),
                "display": title,
                "flag": countries.flag_code(code) or "",
                "search": code,
            }
        if self.lookup_choices:
            yield {
                "selected": self.value() == self.UNKNOWN,
                "query_string": changelist.get_query_string({self.parameter_name: self.UNKNOWN}),
                "display": "Не определён",
                "flag": "",
                "search": "не определён неизвестен пусто",
            }


class PlacementSellerFilter(MultiChoiceFilter):
    """Тот же выбор галочками, что у площадок; продавец совпадает с колонкой —
    продавец самого размещения, а не рабочей цены площадки (ADR-073)."""

    title = "продавец"
    parameter_name = "seller"
    _counted: dict[int, int] | None = None

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            (str(pk), name)
            for pk, name in Seller.objects.order_by("name").values_list("pk", "name")
        ]

    def narrow(self, queryset: Any, values: list[str]) -> Any:
        if not all(value.isdigit() for value in values):
            return queryset.none()
        return queryset.filter(seller_id__in=[int(value) for value in values])

    def choices(self, changelist: Any) -> Iterator[Any]:
        if self._counted is None:
            rows = changelist.root_queryset
            for spec in changelist.filter_specs:
                if spec is self:
                    continue
                narrowed = spec.queryset(self.request, rows)
                if narrowed is not None:
                    rows = narrowed
            if changelist.query:
                rows, _ = changelist.model_admin.get_search_results(
                    self.request, rows, changelist.query
                )
            self._counted = dict(
                rows.order_by()
                .filter(seller_id__isnull=False)
                .values("seller_id")
                .annotate(total=Count("pk", distinct=True))
                .values_list("seller_id", "total")
            )
        picked = set(self.values())
        items = super().choices(changelist)
        yield next(items)
        for (value, _), choice in zip(self.lookup_choices, items, strict=True):
            count = self._counted.get(int(value), 0)
            if count or value in picked:
                yield {**choice, "display": f"{choice['display']} ({count})"}


@admin.register(Placement)
class PlacementAdmin(RecordAdmin):
    """Размещения. Проверка индексации без перезагрузки страницы — кнопка ↻ в
    колонке «в индексе», действие «Проверить индексацию» для отмеченных строк и
    кнопка в карточке. Скрипт `seo/indexation.js` ставит пачку проверок одним
    запросом (`check_indexation_batch_view`), спрашивает их состояние тоже
    одним (`indexation_status_view`) и показывает ход и итог всплывающим
    окном. Без скрипта кнопка в карточке и действие работают с переходом.

    В панели (E9-11) площадка и продукт — в заголовке, а не полями: правят их
    на полной странице. Кнопка проверки там — в группе «Проверки».
    """

    panel = True
    form = PlacementForm
    list_display = (
        "site_link",
        "seller_cell",
        "status_link",
        "dr_cell",
        "traffic_cell",
        "top_geo_cell",
        "placement_type",
        "ordered_day",
        "published_day",
        "publication_price_cell",
        "announce_price_cell",
        "writing_price_cell",
        "paid_cell",
        "link_type_cell",
        "indexed_cell",
        "indexed_at_cell",
        "skip_checks",
        "other_products_cell",
    )
    # Запись открывает домен в первой колонке (`site_link`), а не обёртка Django:
    # рядом с ним свои значки, а вложенные ссылки недопустимы (E1-20).
    list_display_links = None
    list_filter = (
        FrameProductFilter,
        PlacementRegionFilter,
        PlacementSellerFilter,
        PublishedMonthFilter,
        # Статус и «Период ▾» — полосой над списком (ADR-073).
        status_chips.PlacementStatusFilter,
        InvoiceFilter,
        "placement_type",
        "link_type",
        "is_indexed",
        "skip_checks",
    )
    search_fields = ("site__domain", "article_url", "collaborator_order_id")
    # Столбик доменов — размещения ровно этих площадок; одно слово — как раньше,
    # часть домена, адрес статьи или номер заказа.
    domain_search = "site__domain"
    list_select_related = ("site", "product", "seller", "site__price__seller", "site__latest")
    autocomplete_fields = ("site", "seller")
    # «В индексе» и время проверки пишет проверка; результат, увиденный
    # человеком, — запись в журнале проверок (E1-09), а не правка поля.
    readonly_fields = (
        "is_indexed",
        "indexed_checked_at",
        "run_id",
        "created_at",
        "updated_at",
        "indexation_history",
        "indexation_button",
        "status_history",
        "status_date",
        "extra_data",
        "paid_from_invoice",
        "offer_info",
    )
    fieldsets = (
        (None, {"fields": ("site", "product")}),
        # История — сразу под кнопками статуса (E1-13); у новой записи её нет.
        (None, {"fields": ("status", "status_date", "status_history")}),
        # По одному полю в строке (сумма с валютой — вместе): панель — по ширине
        # содержимого, без пустого места справа (пользователь, 03.10.2026).
        (
            REQUEST,
            {
                "fields": (
                    "placement_type",
                    "seller",
                    "offer_info",
                    "employee",
                    "collaborator_order_id",
                    "ordered_at",
                    ("price_paid_cents", "currency"),
                    "link_type",
                    "ad_label_requested",
                )
            },
        ),
        # Кто пишет статью, сколько ему платят и где черновик (ADR-072).
        (
            ARTICLE,
            {"fields": ("copywriter", ("copywriter_cents", "copywriter_currency"), "draft_url")},
        ),
        (
            PUBLICATION,
            {
                "fields": (
                    "article_url",
                    "published_at",
                    "announce_on_homepage",
                    "clicks_from_homepage",
                )
            },
        ),
        (
            CHECKS,
            {"fields": ("is_indexed", "indexed_checked_at", "skip_checks", "indexation_history")},
        ),
        (COMMENT, {"fields": ("comment",)}),
        (SERVICE, {"classes": ("collapse",), "fields": ("run_id", "created_at", "updated_at")}),
    )
    inlines = (PlacementLinkInline,)
    action_form = StatusActionForm
    actions = (
        "set_status_action",
        "check_indexation_action",
        "skip_checks_action",
        "resume_checks_action",
        "invoice_for_selected_action",
    )

    class Media:
        js = (
            Js("seo/indexation.js"),
            Js("seo/invoices.js"),
            # Выбор списка анкоров с флагами (ADR-075) — до скрипта анкоров.
            Js("seo/country-picker.js"),
            Js("seo/anchors.js"),
            Js("seo/copywriter-pick.js"),
        )
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            # offers.css — «прочие данные из файла» в том же виде, что в карточке площадки.
            "all": (
                Css("seo/indexation.css"),
                Css("seo/status-history.css"),
                Css("seo/offers.css"),
                Css("flags/sprite-hq.css"),
                Css("seo/invoices.css"),
                Css("seo/anchors.css"),
                Css("seo/placement-tabs.css"),
                Css("seo/inline-remove.css"),
            )
        }

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, Any]:
        initial = super().get_changeform_initial_data(request)
        # Сотрудник — тот, кто заводит (ADR-073): не выбирать себя каждый раз.
        initial.setdefault("employee", request.user.pk)
        try:
            site_id = int(initial.get("site", ""))
        except (TypeError, ValueError):
            return initial
        site = Site.objects.select_related("price").filter(pk=site_id).first()
        if site is not None and site.price is not None:
            initial.setdefault("seller", site.price.seller_id)
            initial.setdefault("placement_type", site.price.placement_type)
            if str(initial.get("seller")) == str(site.price.seller_id):
                initial.setdefault("link_type", site.price.link_type)
        return initial

    def formfield_for_foreignkey(
        self, db_field: Any, request: HttpRequest, **kwargs: Any
    ) -> "forms.ModelChoiceField[Any] | None":
        if db_field.name == "product":
            # Завести и перенести размещение можно только в рабочем продукте шапки.
            working = working_product_id(request)
            kwargs["queryset"] = (
                Product.objects.filter(pk=working)
                if working is not None
                else Product.objects.none()
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    # ---------- Чужой продукт — только просмотр (ADR-073) ----------

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return own_product(request, obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return own_product(request, obj) and super().has_delete_permission(request, obj)

    def get_actions(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        # Удаляют по одному — корзиной на вкладке размещения, с подтверждением:
        # отмеченная строка не говорит, какое из размещений площадки удалить.
        actions = super().get_actions(request, *args, **kwargs)
        actions.pop("delete_selected", None)
        return actions

    def response_action(
        self, request: HttpRequest, queryset: QuerySet[Any], *args: Any, **kwargs: Any
    ) -> HttpResponse | None:
        # «Выбрать все N» — строки списка, по одной на площадку (ADR-073): старые
        # размещения тех же площадок действие не задевает. Отмеченные — как есть.
        if request.POST.get("select_across") == "1":
            queryset = repeat.one_per_site(queryset)
        return super().response_action(request, queryset, *args, **kwargs)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Placement]:
        # Колонка «заплачено»: из счёта ли сумма и оплачен ли он — без запроса на строку.
        queryset: QuerySet[Placement] = (
            super().get_queryset(request).select_related(*self.list_select_related)
        )
        queryset = queryset.annotate(
            # Предложение размещения (ADR-073): продавец и цены строки — его, а не
            # рабочей цены площадки; сортировка цен — по нему же.
            deal_offer_id=repeat.offer_id(),
            deal_placement_cents=_deal_price("placement_cents"),
            deal_announce_cents=_deal_price("announce_cents"),
            deal_writing_cents=_deal_price("writing_cents"),
            invoice_issued=_in_invoice(InvoiceStatus.ISSUED),
            invoice_paid=_in_invoice(InvoiceStatus.PAID),
        )
        # Оценка площадки (E1-21): среднее и своя — подзапросами, представления тут нет.
        return rating.with_mine(rating.with_summary(queryset), request.user)

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        if export.is_export(request):
            return export.ExportChangeList
        return PlacementChangeList

    def get_urls(self) -> list[URLPattern]:
        own = [
            path(
                "export-designer/",
                self.admin_site.admin_view(
                    require_http_methods(["GET", "POST"])(self.export_designer_view)
                ),
                name="placements_placement_export_designer",
            ),
            path(
                "export/<str:fmt>/",
                self.admin_site.admin_view(require_http_methods(["GET", "POST"])(self.export_view)),
                name=export.url_name(self),
            ),
            path(
                "check-indexation/",
                self.admin_site.admin_view(require_POST(self.check_indexation_batch_view)),
                name="placements_placement_check_indexation_batch",
            ),
            path(
                "indexation-status/",
                self.admin_site.admin_view(require_GET(self.indexation_status_view)),
                name="placements_placement_indexation_status",
            ),
            path(
                "<int:object_id>/check-indexation/",
                self.admin_site.admin_view(require_POST(self.check_indexation_view)),
                name="placements_placement_check_indexation",
            ),
            # Адрес из поля «Адрес страницы» над списком (E9-12).
            path(
                "check-url/",
                self.admin_site.admin_view(require_POST(self.check_url_view)),
                name="placements_placement_check_url",
            ),
            path(
                "check-url/status/",
                self.admin_site.admin_view(require_GET(self.check_url_status_view)),
                name="placements_placement_check_url_status",
            ),
        ]
        return own + super().get_urls()

    def export_designer_view(self, request: HttpRequest) -> HttpResponse:
        return export_designer_view.view(self, request)

    def export_choices(self, request: HttpRequest) -> list[export.Choice]:
        """Галочки окна выгрузки (тег `export_tools`) — колонки листа «Размещения»."""
        return placement_export.choices()

    def export_view(self, request: HttpRequest, fmt: str) -> HttpResponse:
        """Отчёт: окно «Выгрузить» — отобранное в списке или отмеченные строки (E1-06)."""

        def build(
            changelist: ChangeList, queryset: QuerySet[Any], wanted: AbstractSet[str]
        ) -> tuple[list[export.Sheet], str]:
            return placement_export.sheets(queryset, wanted), _export_name(request)

        return export.export_view(self, request, fmt, self.export_choices(request), build)

    def check_indexation_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        """Кнопка в карточке без скрипта: ставит проверку и возвращает в карточку."""
        placement = get_object_or_404(Placement, pk=object_id)
        change_url = reverse("admin:placements_placement_change", args=[placement.pk])
        if not self.has_change_permission(request, placement):
            return HttpResponseRedirect(change_url)
        if not placement.article_url:
            self.message_user(request, "Нет адреса статьи — проверять нечего.", messages.WARNING)
        else:
            _queue_checks([placement.pk])
            self.message_user(request, QUEUED, messages.SUCCESS)
        return HttpResponseRedirect(change_url)

    def check_indexation_batch_view(self, request: HttpRequest) -> JsonResponse:
        """Ставит проверки размещениям `ids`: {"tasks": {id: id задачи}, "skipped": [...]}."""
        if not self.has_change_permission(request):
            return JsonResponse({"error": "Нет прав на изменение размещений."}, status=403)
        try:
            ids = [int(value) for value in request.POST.getlist("ids")]
        except ValueError:
            return JsonResponse({"error": "Неверный список размещений."}, status=400)
        checkable = list(
            Placement.objects.filter(pk__in=ids)
            .exclude(article_url__isnull=True)
            .exclude(article_url="")
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        tasks = {
            str(pk): task for pk, task in zip(checkable, _queue_checks(checkable), strict=True)
        }
        return JsonResponse({"tasks": tasks, "skipped": sorted(set(ids) - set(checkable))})

    def indexation_status_view(self, request: HttpRequest) -> JsonResponse:
        """Как идут проверки `?t=<id размещения>:<id задачи>` и что сейчас у размещений.

        `history=1` — ещё и история проверок (для карточки).
        """
        if not self.has_view_permission(request):
            return JsonResponse({"error": "Нет прав на просмотр размещений."}, status=403)
        pairs: dict[int, str] = {}
        for value in request.GET.getlist("t"):
            raw_id, _, task_id = value.partition(":")
            if raw_id.isdigit():
                pairs[int(raw_id)] = task_id
        placements = Placement.objects.in_bulk(list(pairs))
        states = _task_states([task for task in pairs.values() if task])
        with_history = request.GET.get("history") == "1"
        items: dict[str, dict[str, Any]] = {}
        for placement_id, task_id in pairs.items():
            placement = placements.get(placement_id)
            if placement is None:
                continue
            state = dict(states.get(task_id, {"state": "queued" if task_id else "unknown"}))
            state.update(
                {
                    "indexed": placement.is_indexed,
                    "indexed_html": _indexed_icon(placement.is_indexed),
                    "checked_at": _when(placement.indexed_checked_at),
                }
            )
            if with_history:
                state["history_html"] = self.indexation_history(placement)
            items[str(placement_id)] = state
        return JsonResponse({"items": items})

    def check_url_view(self, request: HttpRequest) -> JsonResponse:
        """Ставит проверку адреса из поля над списком: {"key", "task", "url"} (E9-12).

        Тот же поиск, что у ↻, но ничего не записывает: итог показывает окошко.
        """
        if not self.has_view_permission(request):
            return JsonResponse({"error": "Нет прав на просмотр размещений."}, status=403)
        try:
            url = page_url(request.POST.get("url", ""))
        except ValidationError as error:
            # 200, а не 400: это ответ человеку, а 4xx браузер пишет в консоль ошибкой.
            return JsonResponse({"error": error.messages[0]})
        key = uuid4().hex
        # Нажатие — начало цепочки (ADR-025), как у кнопки ↻.
        with bind_run_id(new_run_id()):
            task = check_url_indexation.delay(url, key).id
        return JsonResponse({"key": key, "task": task, "url": url})

    def check_url_status_view(self, request: HttpRequest) -> JsonResponse:
        """Итог проверки адреса `?key=…&task=…`: done с `indexed` или как идёт задача."""
        if not self.has_view_permission(request):
            return JsonResponse({"error": "Нет прав на просмотр размещений."}, status=403)
        key = request.GET.get("key", "")
        result = cache.get(url_result_key(key)) if key else None
        if result is not None:
            return JsonResponse({"state": "done", **result})
        task = request.GET.get("task", "")
        states = _task_states([task], task_name="check_url_indexation") if task else {}
        return JsonResponse(states.get(task, {"state": "queued"}))

    def panel_title(self, obj: Placement) -> str:
        # Статус — кнопками в самой форме, продукт — рабочий, из шапки (ADR-063):
        # в заголовке оба лишние.
        return str(obj.site.domain)

    @admin.display(description="площадка", ordering="site__domain")
    def site_link(self, obj: Placement) -> SafeString:
        """Домен открывает само размещение, рядом — значки площадки (E1-20).

        Ссылка ведёт на запись этого же списка, поэтому панель открывает её сама
        (`seo/panel.js`), а Ctrl — отдельной страницей. Своя ссылка в ячейке
        нужна из-за значков: вкладывать их в обёртку Django нельзя. Корзины в
        строке нет (ADR-073): у площадки бывает несколько размещений, удаляют
        нужное — на его вкладке в панели.
        """
        stars = rating_html(
            reverse("admin:sites_site_rate", args=[obj.site_id]),
            getattr(obj, "rating_avg", None),
            getattr(obj, "rating_count", 0) or 0,
            getattr(obj, rating.MINE, None),
        )
        return format_html(
            '{}<a href="{}" title="Карточка размещения">{}</a>{}',
            stars,
            reverse("admin:placements_placement_change", args=[obj.pk]),
            obj.site.domain,
            domain_tools_html(obj.site.domain),
        )

    @admin.display(description="другие продукты")
    def other_products_cell(self, obj: Placement) -> SafeString | str:
        found: list[other_products.Row] = getattr(obj, "page_products", [])
        return other_products.cell(other_products.others(found, obj.product_id))

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj is not None and invoices.invoiced([obj.pk]):
            # «Заплачено» из счёта — текстом со ссылкой на счёт, руками не правится.
            paid = ("price_paid_cents", "currency")
            fieldsets = [
                (
                    name,
                    {
                        **options,
                        "fields": tuple(
                            "paid_from_invoice" if field == paid else field
                            for field in options["fields"]
                        ),
                    },
                )
                for name, options in fieldsets
            ]
        if obj is not None and obj.extra:
            # Прочие данные из файла размещений — перед «Служебным», только если есть.
            fieldsets.insert(len(fieldsets) - 1, (FROM_FILE, {"fields": ("extra_data",)}))
        if obj is None:
            # Новое размещение на площадке с ценами (ADR-073): «Через кого» вместо
            # продавца, услуги и типа ссылки — их даёт выбранное предложение.
            offers = _offer_site(request) is not None
            result: list[Any] = []
            for name, options in fieldsets:
                kept: tuple[Any, ...] = _without(options["fields"], "status_history", "offer_info")
                if name == REQUEST and offers:
                    picked = _without(kept, "placement_type", "seller", "link_type")
                    kept = ("offer", "make_working", *picked)
                result.append((name, {**options, "fields": kept}))
            return result
        if not self.in_panel(request):
            return fieldsets
        # Панель: площадка и продукт — в заголовке; кнопка проверки — в «Проверках»
        # (кнопки над формой у панели нет).
        result = []
        for name, options in fieldsets:
            fields = options["fields"]
            if fields == ("site", "product"):
                continue
            if name == CHECKS:
                # Кнопка — сразу под «Индексация проверена».
                at = fields.index("indexed_checked_at") + 1
                options = {**options, "fields": (*fields[:at], "indexation_button", *fields[at:])}
            result.append((name, options))
        return result

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: Any = None,
    ) -> Any:
        context["after_links"] = AFTER_LINKS
        # «Анкоры продукта» над ссылками (E3-05): итоги, доли и рекомендации —
        # без анкоров, которые уже стоят в этом размещении.
        product_id = obj.product_id if obj is not None else _initial_product(context)
        used = (
            list(obj.links.exclude(keyword=None).values_list("keyword_id", flat=True))
            if obj is not None
            else []
        )
        lists = AnchorList.of_product(product_id) if product_id else []
        choice = _anchor_choice(context, obj, lists)
        context.update(anchor_views.summary_context(product_id, used, choice=choice, lists=lists))
        product = Product.objects.filter(pk=product_id).first() if product_id else None
        if product is not None:
            context["anchor_dialog"] = anchor_views.dialog_context(
                product, lists, choice.anchor_list
            )
            context["anchor_product"] = product
        if obj is not None:
            # Чужие продукты — ссылками в шапке панели, только просмотр (E1-19,
            # ADR-073); карточка площадки — вкладка «Площадка».
            found = other_products.by_site([obj.site_id]).get(obj.site_id, [])
            context["panel_links"] = [
                (f"Размещение {name}", other_products.card_url(pk), True)
                for name, pk in other_products.others(found, obj.product_id)
            ]
        target = _tabs_target(request, obj)
        if target is not None:
            site_id, product_id = target
            own = product_id == working_product_id(request)
            context["panel_tabs"] = repeat.tabs(
                site_id,
                product_id,
                obj.pk if obj is not None else "new",
                can_add=own and self.has_add_permission(request),
                can_delete=own and self.has_delete_permission(request),
            )
        return super().render_change_form(request, context, add, change, form_url, obj)

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> Any:
        form = super().get_form(request, obj, change, **kwargs)
        if obj is None and _offer_site(request) is not None:
            # Класс формы свой на каждый запрос (modelform_factory) — метка только у него.
            form.offer_shown = True  # type: ignore[attr-defined]
        if obj is None and self.in_panel(request) and _tabs_target(request, None) is not None:
            # Новое размещение из вкладок площадки: площадка и продукт уже заданы —
            # в панели они не поля, а заголовок.
            for name in ("site", "product"):
                if name in form.base_fields:
                    form.base_fields[name].widget = forms.HiddenInput()
        return form

    def panel_add_title(self, request: HttpRequest) -> str:
        site_id = _int(request.GET.get("site"))
        site = Site.all_objects.filter(pk=site_id).first() if site_id is not None else None
        if site is None:
            return super().panel_add_title(request)
        return f"{site.domain} — новое размещение"

    def save_model(self, request: HttpRequest, obj: Placement, form: Any, change: bool) -> None:
        if change and {"seller", "placement_type"} & set(form.changed_data):
            # Продавца или услугу сменили руками (ADR-073) — предложение размещения
            # (цены строки и план «Экономики») — текущее у нового продавца.
            obj.price = repeat.current_offer(obj.site_id, obj.seller_id, obj.placement_type)
        super().save_model(request, obj, form, change)
        if not change:
            # Завёл человек — «Отбрасываю» и «Отказ» площадки снимаются (ADR-073).
            statuses.reopen(obj.site_id, obj.product_id, placement_id=obj.pk)
        offer = obj.price
        if not change and offer is not None and form.cleaned_data.get("make_working"):
            # «Сделать эту цену рабочей» — та же смена, что в карточке площадки:
            # с заметкой в истории.
            set_working_price(offer, author=request.user)

    def response_add(
        self, request: HttpRequest, obj: Any, post_url_continue: str | None = None
    ) -> HttpResponse:
        if self.in_panel(request) and _tabs_target(request, None) is not None:
            # Завели из вкладок площадки — панель сразу показывает новое размещение.
            next_url = reverse("admin:placements_placement_change", args=[obj.pk])
            return self._panel_saved(request, obj, "добавлено", next=next_url)
        return super().response_add(request, obj, post_url_continue)

    @admin.display(description="предложение продавца")
    def offer_info(self, obj: Placement) -> SafeString | str:
        """Через какое предложение взяли (ADR-073) и чем оно отличается от рабочей цены."""
        if obj.pk is None:
            return "—"
        offer = obj.price or repeat.offers_of([obj]).get(obj.pk)
        if offer is None:
            return (
                "нет — у продавца размещения нет цены на эту услугу: «Экономика»"
                " посчитает по «Заплачено», когда его впишете"
            )
        text = format_html(
            '{} <span class="seo-sub">прайс от {}</span>',
            describe(offer),
            f"{timezone.localtime(offer.checked_at):%d.%m.%Y}",
        )
        if obj.price is None:
            # Размещение до ADR-073: предложение не запоминалось — цена его
            # продавца на дату заведения.
            text = format_html(
                '{}<div class="seo-sub">Цена продавца размещения на день, когда его завели.</div>',
                text,
            )
        if obj.site.price_id == offer.pk:
            return text
        return format_html(
            '{}<div class="seo-sub">Рабочая цена площадки сейчас другая — размещение'
            " остаётся с этим продавцом и ценой; план «Экономики» по ним, пока нет"
            " «Заплачено». Сменить продавца — полем «Продавец».</div>",
            text,
        )

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        placement = self.get_object(request, object_id)
        extra = dict(extra_context or {})
        if placement is not None and placement.article_url:
            extra["check_indexation_url"] = reverse(
                "admin:placements_placement_check_indexation", args=[placement.pk]
            )
            extra["indexation_urls"] = _batch_urls()
            extra["indexation_name"] = placement.site.domain
        return super().change_view(request, object_id, form_url, extra)

    @admin.display(description="статус", ordering="status")
    def status_link(self, obj: Placement) -> SafeString:
        # Фишки размещений площадки (ADR-073): каждая открывает своё размещение.
        cell: status_chips.Cell | None = getattr(obj, "page_cell", None)
        if cell is not None and cell.chips:
            return status_chips.cell_html(cell)
        # Щелчок — панель с формой размещения, статус в ней первым (seo/panel.js);
        # с Ctrl и без скрипта — полная страница.
        url = reverse("admin:placements_placement_change", args=[obj.pk])
        # data-seo-field — после записи в панели статус меняется здесь сразу.
        return format_html(
            '<a href="{}" title="Сменить статус" data-seo-field="status">{}</a>',
            url,
            obj.get_status_display(),
        )

    @admin.display(description="когда")
    def status_date(self, obj: Placement) -> SafeString | str:
        """Дата текущего статуса под кнопками (E1-22): одна, не обе.

        «Заявка отправлена» — когда ушла заявка, «Размещено» — когда вышла
        статья. У остальных статусов своей даты нет, и строка пустая: старая
        дата рядом с новым статусом читалась бы как дата этого статуса
        (просьба пользователя 07.10.2026). Сами поля остаются в своих
        разделах — там их и правят.
        """
        field = STATUS_STAMPS.get(obj.status)
        when = getattr(obj, field, None) if field else None
        if when is None:
            return "—"
        return format_html(
            '<span class="seo-sub">{}</span> {}',
            obj.get_status_display(),
            f"{timezone.localtime(when):%d.%m.%Y}",
        )

    @admin.display(description="отправлена", ordering="ordered_at")
    def ordered_day(self, obj: Placement) -> str:
        """Когда заявка ушла вебмастеру (E1-22). День без времени, как и остальные."""
        if obj.ordered_at is None:
            return ""
        return f"{timezone.localtime(obj.ordered_at):%d.%m.%Y}"

    @admin.display(description="опубликовано", ordering="published_at")
    def published_day(self, obj: Placement) -> str:
        # День без времени — как в форме: время публикации никто не вводит.
        if obj.published_at is None:
            return ""
        return f"{timezone.localtime(obj.published_at):%d.%m.%Y}"

    @admin.display(description="проверить сейчас")
    def indexation_button(self, obj: Placement) -> SafeString | str:
        """Кнопка проверки внутри формы (панель): без формы вокруг — форма в форме нельзя."""
        if obj.pk is None or not obj.article_url:
            return "Нет адреса статьи — проверять нечего."
        check_url, status_url = _batch_urls()
        return format_html(
            '<button type="button" class="seo-btn" data-indexation-button data-check-url="{}"'
            ' data-status-url="{}" data-placement="{}" data-name="{}"'
            ' title="Поиск в Google по адресу статьи, около 0,1 цента">'
            "Проверить индексацию</button>",
            check_url,
            status_url,
            obj.pk,
            obj.site.domain,
        )

    @admin.display(description="в индексе", ordering="is_indexed")
    def indexed_cell(self, obj: Placement) -> str:
        value = format_html(
            '<span class="seo-indexed" data-indexed="{}">{}</span>',
            obj.pk,
            _indexed_icon(obj.is_indexed),
        )
        if not obj.article_url:
            return value
        check_url, status_url = _batch_urls()
        return format_html(
            '{} <button type="button" class="seo-index-check" data-check-url="{}"'
            ' data-status-url="{}" data-placement="{}" data-name="{}" title="Проверить'
            ' индексацию (поиск в Google, около 0,1 цента)"'
            ' aria-label="Проверить индексацию">↻</button>',
            value,
            check_url,
            status_url,
            obj.pk,
            obj.site.domain,
        )

    @admin.display(description="индексация проверена", ordering="indexed_checked_at")
    def indexed_at_cell(self, obj: Placement) -> str:
        return format_html(
            '<span class="seo-indexed-at" data-indexed-at="{}">{}</span>',
            obj.pk,
            _when(obj.indexed_checked_at),
        )

    @admin.action(description="Поставить статус…", permissions=["change"])
    def set_status_action(self, request: HttpRequest, queryset: QuerySet[Placement]) -> None:
        """Статус отмеченным размещениям — выбором рядом с действием (ADR-062).

        Через `save()`, а не `update()`: по статусу размещения двигается статус
        площадки у продукта, и это делает сам `Placement.save()`.
        """
        status = request.POST.get("status") or ""
        if status not in WorkStatus.values:
            self.message_user(request, "Выберите статус рядом с действием.", messages.WARNING)
            return
        changed = 0
        # Только рабочий продукт шапки (ADR-073): чужие размещения не трогаем.
        working = working_product_id(request)
        queryset = queryset.filter(product_id=working) if working is not None else queryset.none()
        with stamped(source=StatusSource.FORM):
            for placement in queryset.exclude(status=status):
                placement.status = WorkStatus(status)
                placement.save(update_fields=["status", "updated_at"])
                changed += 1
        label = WorkStatus(status).label
        self.message_user(request, f"Статус «{label}» поставлен: {changed}.", messages.SUCCESS)

    @admin.action(description="Проверить индексацию")
    def check_indexation_action(self, request: HttpRequest, queryset: QuerySet[Placement]) -> None:
        ids = list(
            queryset.exclude(article_url__isnull=True)
            .exclude(article_url="")
            .values_list("pk", flat=True)
        )
        skipped = queryset.count() - len(ids)
        if ids:
            _queue_checks(ids)
            self.message_user(
                request,
                f"Проверка индексации поставлена в очередь: {len(ids)}."
                " Результаты появятся через несколько секунд.",
                messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request, f"Без адреса статьи, не проверяются: {skipped}.", messages.WARNING
            )

    @admin.action(description="Не проверять по расписанию")
    def skip_checks_action(self, request: HttpRequest, queryset: QuerySet[Placement]) -> None:
        count = queryset.filter(skip_checks=False).update(
            skip_checks=True, updated_at=timezone.now()
        )
        self.message_user(request, f"Убрано из проверок по расписанию: {count}.")

    @admin.action(description="Снова проверять по расписанию")
    def resume_checks_action(self, request: HttpRequest, queryset: QuerySet[Placement]) -> None:
        count = queryset.filter(skip_checks=True).update(
            skip_checks=False, updated_at=timezone.now()
        )
        self.message_user(request, f"Снова проверяется по расписанию: {count}.")

    @admin.display(description="продавец", ordering="seller__name")
    def seller_cell(self, obj: Placement) -> SafeString | str:
        """Продавец размещения — свой, зафиксирован при заведении (ADR-073).

        Смена рабочей цены площадки его не меняет — менять руками в карточке
        размещения. Рабочая цена сейчас у другого продавца — подсказкой под именем.
        """
        seller = obj.seller
        working = obj.site.price
        if seller is None:
            return ""
        link = format_html(
            '<a href="{}" data-panel title="Карточка продавца">{}</a>',
            reverse("admin:sites_seller_change", args=[seller.pk]),
            seller.name,
        )
        if working is not None and working.seller_id != seller.pk:
            return format_html(
                '{}<div class="seo-sub" title="Рабочая цена площадки сейчас у другого'
                ' продавца; продавец размещения от этого не меняется">рабочая цена — {}</div>',
                link,
                working.seller.name,
            )
        return link

    @admin.display(description="DR", ordering="site__latest__dr")
    def dr_cell(self, obj: Placement) -> int | str:
        row: SiteLatest | None = getattr(obj.site, "latest", None)
        return row.dr if row is not None and row.dr is not None else ""

    @admin.display(description="трафик", ordering="site__latest__organic_traffic")
    def traffic_cell(self, obj: Placement) -> int | str:
        row: SiteLatest | None = getattr(obj.site, "latest", None)
        return row.organic_traffic if row is not None and row.organic_traffic is not None else ""

    @admin.display(description="топ регион", ordering="site__latest__top_geo_traffic")
    def top_geo_cell(self, obj: Placement) -> SafeString | str:
        row: SiteLatest | None = getattr(obj.site, "latest", None)
        if row is None or not row.top_geo:
            return ""
        return format_html(
            '<span title="{}">{} {}</span>',
            countries.name(row.top_geo),
            row.top_geo.upper(),
            "" if row.top_geo_traffic is None else row.top_geo_traffic,
        )

    @admin.display(description="цена публикации", ordering="deal_placement_cents")
    def publication_price_cell(self, obj: Placement) -> str:
        price = _deal_offer(obj)
        return money(price.placement_cents, price.currency) if price is not None else ""

    @admin.display(description="цена анонса", ordering="deal_announce_cents")
    def announce_price_cell(self, obj: Placement) -> str:
        price = _deal_offer(obj)
        return money(price.announce_cents, price.currency) if price is not None else ""

    @admin.display(description="цена написания", ordering="deal_writing_cents")
    def writing_price_cell(self, obj: Placement) -> str:
        price = _deal_offer(obj)
        return money(price.writing_cents, price.currency) if price is not None else ""

    @admin.display(description="тип ссылки", ordering="link_type", empty_value="")
    def link_type_cell(self, obj: Placement) -> str:
        return obj.link_type or ""

    @admin.display(description="заплачено", ordering="price_paid_cents")
    def paid_cell(self, obj: Placement) -> SafeString | str:
        amount = money(obj.price_paid_cents, obj.currency or "EUR")
        if getattr(obj, "invoice_issued", False):
            note = "счёт не оплачен"
        elif getattr(obj, "invoice_paid", False):
            note = "счёт оплачен"
        else:
            return amount
        return format_html('{}<div class="seo-sub">{}</div>', amount, note)

    @admin.display(description="заплачено")
    def paid_from_invoice(self, obj: Placement) -> SafeString | str:
        """Сумма из счетов размещения и сами счета — ссылками, открываются в панели."""
        if obj.pk is None:
            return "—"
        others = invoices.invoice_refs([obj.pk])
        rows = [
            (
                reverse("admin:placements_invoice_change", args=[other.invoice_id]),
                other.label,
                InvoiceStatus(other.status).label.lower(),
            )
            for other in others
        ]
        return format_html(
            '<div class="seo-paid-invoice"><b>{}</b> — из счёта, руками не правится{}</div>',
            money(obj.price_paid_cents, obj.currency or "EUR"),
            format_html_join("", '<div><a href="{}" data-panel>{}</a> · {}</div>', rows),
        )

    @admin.action(description="Счёт на отмеченные")
    def invoice_for_selected_action(
        self, request: HttpRequest, queryset: QuerySet[Placement]
    ) -> HttpResponse | None:
        """Форма нового счёта с отмеченными размещениями (со скриптом — панелью)."""
        limit = INVOICE_MAX_PLACEMENTS + 1
        ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:limit])
        if len(ids) > INVOICE_MAX_PLACEMENTS:
            self.message_user(
                request,
                f"В одном счёте — не больше {INVOICE_MAX_PLACEMENTS} размещений:"
                " отметьте только те, что закрывает счёт.",
                messages.WARNING,
            )
            return None
        url = reverse("admin:placements_invoice_add")
        return HttpResponseRedirect(f"{url}?placements={','.join(map(str, ids))}")

    @admin.display(description="история статуса")
    def status_history(self, obj: Placement) -> SafeString | str:
        """Смены статуса, новые сверху: когда, с какого на какой, кто и откуда (ADR-049)."""
        rows = placement_history(obj.pk) if obj.pk is not None else []
        if not rows:
            return "—"
        return format_html(
            '<ul class="seo-status-list">{}</ul>',
            format_html_join(
                "",
                '<li><span class="seo-sub">{}</span> {} · <span class="seo-sub">{}</span></li>',
                ((row.when, row.change, row.who) for row in rows),
            ),
        )

    @admin.display(description="прочие данные из файла")
    def extra_data(self, obj: Placement) -> SafeString | str:
        """Колонки файла размещений без своего поля: заголовок → значение (ADR-051)."""
        if not obj.extra:
            return "—"
        # Как прочие данные предложения в карточке площадки.
        return format_html(
            '<div class="seo-kv">{}</div>',
            format_html_join("", "<span>{}: <b>{}</b></span>", obj.extra.items()),
        )

    @admin.display(description="проверки индексации")
    def indexation_history(self, obj: Placement) -> str:
        if obj.pk is None:
            return "—"
        checks = Check.objects.filter(
            entity_type=ENTITY_TYPE, entity_id=obj.pk, check_type=CHECK_TYPE
        ).order_by("-checked_at", "-id")[:HISTORY_LIMIT]
        rows = [_history_row(check, obj.article_url) for check in checks]
        if not rows:
            return "Ещё не проверялась."
        return format_html(
            "<table><thead><tr><th>Когда</th><th>Результат</th><th>Кто</th>"
            "<th>Следующая</th></tr></thead><tbody>{}</tbody></table>",
            format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows),
        )


def _export_name(request: HttpRequest) -> str:
    """«Размещения Convertio сентябрь 2026»: рабочий продукт и месяц фильтра, без месяца — дата."""
    parts = ["Размещения"]
    # Продукт — рабочий: строки списка под ним, фильтр продукта их не меняет (ADR-063).
    product_id = working_product_id(request)
    if product_id is not None:
        name = Product.objects.filter(pk=product_id).values_list("name", flat=True).first()
        if name:
            parts.append(name)
    value = request.GET.get(PublishedMonthFilter.parameter_name)
    month = parse_month(value)
    if month is not None:
        parts.append(month_name(month))
    elif value == PublishedMonthFilter.NO_DATE:
        parts.append("без даты")
    else:
        parts.append(f"{timezone.localdate():%d.%m.%Y}")
    return " ".join(parts)


def _without(fields: Sequence[Any], *names: str) -> tuple[Any, ...]:
    return tuple(field for field in fields if field not in names)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _deal_price(field: str) -> Subquery:
    """Цена из предложения размещения (`repeat.offer_id`) — для сортировки колонок цены."""
    return Subquery(SitePrice.objects.filter(pk=OuterRef("deal_offer_id")).values(field)[:1])


def _deal_offer(obj: Placement) -> SitePrice | None:
    """Предложение строки — размещения (ADR-073). Нет — цены пустые: цену чужого
    продавца (рабочую цену площадки) не показываем."""
    offer: SitePrice | None = getattr(obj, "page_offer", None)
    return offer


def _tabs_target(request: HttpRequest, obj: Placement | None) -> tuple[int, int] | None:
    """Площадка и продукт вкладок: у записи — её, у новой — из адреса (?site=&product=)."""
    if obj is not None:
        return obj.site_id, obj.product_id
    site, product = _int(request.GET.get("site")), _int(request.GET.get("product"))
    return (site, product) if site is not None and product is not None else None


def _offer_site(request: HttpRequest) -> int | None:
    """Площадка новой формы, если у неё есть цены, — тогда в форме «Через кого»."""
    # Только адрес формы: на открытии и на записи он один и тот же — поля те же.
    site_id = _int(request.GET.get("site"))
    if site_id is None:
        return None
    if (
        SiteOffer.objects.filter(site_id=site_id).exists()
        or Site.all_objects.filter(pk=site_id, price__isnull=False).exists()
    ):
        return site_id
    return None


def _when(moment: Any) -> str:
    return "—" if moment is None else timezone.localtime(moment).strftime("%d.%m.%Y %H:%M")


def _indexed_icon(value: bool | None) -> str:
    """Значок «в индексе», как у штатных колонок да/нет; пусто — не проверялась."""
    return display_for_value(value, "—", boolean=True)


def _batch_urls() -> tuple[str, str]:
    return (
        reverse("admin:placements_placement_check_indexation_batch"),
        reverse("admin:placements_placement_indexation_status"),
    )


def _task_states(
    task_ids: list[str], task_name: str = "check_indexation"
) -> dict[str, dict[str, Any]]:
    """Состояние проверок по строкам журнала запусков (config/queue.py), одним запросом.

    Строки нет — задача ещё ждёт в очереди (или её отложило ограничение
    скорости): такой задачи в ответе нет. `errors` — неудачные попытки,
    после них будет повтор; `waiting` — пауза, например до полуночи по
    дневному лимиту.
    """
    if not task_ids:
        return {}
    runs = TaskRun.objects.filter(
        task_name=task_name,
        started_at__gte=timezone.now() - STATUS_LOOKBACK,
        payload__task_id__in=task_ids,
    ).order_by("id")
    states: dict[str, dict[str, Any]] = {}
    for run in runs:
        payload = run.payload or {}
        state: dict[str, Any] = {
            "state": run.status,
            "attempt": payload.get("attempt", 1),
            "attempts": MAX_ATTEMPTS,
            "errors": payload.get("errors", []),
        }
        if run.status == TaskStatus.FAILED:
            state["error"] = run.error or "проверка не удалась"
        elif "waiting" in payload:
            state["state"] = "waiting"
            state["waiting"] = {
                "until": _when(datetime.fromisoformat(payload["waiting"]["until"])),
                "reason": payload["waiting"]["reason"],
            }
        states[str(payload.get("task_id"))] = state
    return states


def _history_row(check: Check, current_url: str | None) -> tuple[str, str, str, str]:
    """Строка истории: когда, результат, кто запустил, когда следующая."""
    result = check.result or {}
    if check.status == CheckStatus.OK:
        position = result.get("position")
        outcome = "в индексе" + (f", место {position}" if position else "")
        if result.get("found_by") == "url":
            outcome += " — найдена поиском по адресу, site: её не показал"
    elif check.status == CheckStatus.FAILED:
        outcome = "не в индексе"
        if result.get("alert"):
            outcome += f" {result.get('failing_days', '?')} дн. — оповещение"
        seen = result.get("seen") or []
        if seen:
            outcome += f"; в выдаче: {seen[0]}"
            if len(seen) > 1:
                outcome += f" и ещё {len(seen) - 1}"
    else:
        outcome = check.get_status_display()
    if result.get("url") and result["url"] != current_url:
        outcome += f" — по прежнему адресу {result['url']}"
    if check.performed_by == Performer.HUMAN:
        who = "человек"
        if result.get("source") == "upload":
            # Отметка из файла размещений (ADR-051): какой файл и какая колонка.
            who = f"человек, файл «{result.get('file', '')}», «{result.get('column', '')}»"
    else:
        who = "кнопка" if result.get("manual") else "расписание"
    return (_when(check.checked_at), outcome, who, _when(check.next_check_at))


# Админка счетов — своим модулем; регистрируется при загрузке этого.
from apps.placements import invoice_admin  # noqa: E402, F401
