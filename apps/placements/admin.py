"""Админка блока 2: размещения и ссылки в них.

Удаление отключено: размещение отменяется статусом «Отменено», ссылка
исправляется, но не удаляется.

Проверка индексации (E2-03): кнопка в карточке и действие в списке ставят
проверку в очередь, в карточке — история проверок. «Не проверять» убирает
размещение из проверок по расписанию.

Размещение открывается панелью справа поверх списка (E9-11): щелчок по
площадке или статусу. Форма — `apps/placements/forms.py`: статус кнопками,
поля группами, даты без времени, «Заплачено» в валюте.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar

from django.contrib import admin, messages
from django.contrib.admin.utils import display_for_value
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_POST

from apps.observability.models import Check, CheckStatus, Performer, TaskRun, TaskStatus
from apps.placements.forms import PlacementForm
from apps.placements.indexation import CHECK_TYPE, ENTITY_TYPE
from apps.placements.models import Placement, PlacementLink
from apps.placements.tasks import check_indexation
from apps.sites.status_history import placement_history
from config.admin import NoDeleteAdmin, StackedInline
from config.assets import Css, Js
from config.queue import MAX_ATTEMPTS
from config.run_id import bind_run_id, new_run_id

# Сколько последних проверок индексации показывать в карточке.
HISTORY_LIMIT = 20
QUEUED = "Проверка индексации поставлена в очередь — обновите страницу через несколько секунд."
# Строку запуска проверки ищем среди запусков за это время: кнопку жмут
# и смотрят на результат сразу, повторы идут минуты.
STATUS_LOOKBACK = timedelta(days=1)


class PlacementLinkInline(StackedInline):
    """Ссылки размещения: задание правит человек, остальное — проверка страницы.

    Что на странице (`rel`, позиция, живость, время пропажи), пишут
    краулер и проверка живости (E2-04, E2-05) — в форме только чтение:
    время пропажи пишется один раз. Тест на извлечение до E7-02 делает
    человек.
    """

    model = PlacementLink
    can_delete = False
    autocomplete_fields = ("keyword",)
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
            {
                "fields": (
                    "anchor",
                    "anchor_type",
                    "target_url",
                    "keyword",
                    "link_index",
                    "extraction_test_passed",
                )
            },
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
PUBLICATION = "Публикация"
CHECKS = "Проверки"
COMMENT = "Комментарий"
SERVICE = "Служебное"
AFTER_LINKS = (CHECKS, COMMENT, SERVICE)


@admin.register(Placement)
class PlacementAdmin(NoDeleteAdmin):
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
        "site",
        "product",
        "status_link",
        "placement_type",
        "published_day",
        "indexed_cell",
        "indexed_at_cell",
        "skip_checks",
    )
    list_filter = ("product", "status", "placement_type", "is_indexed", "skip_checks")
    search_fields = ("site__domain", "article_url", "collaborator_order_id")
    list_select_related = ("site", "product")
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
    )
    fieldsets = (
        (None, {"fields": ("site", "product")}),
        # История — сразу под кнопками статуса (E1-13); у новой записи её нет.
        (None, {"fields": ("status", "status_history")}),
        # По одному полю в строке (сумма с валютой — вместе): панель — по ширине
        # содержимого, без пустого места справа (пользователь, 03.10.2026).
        (
            REQUEST,
            {
                "fields": (
                    "placement_type",
                    "seller",
                    "employee",
                    "collaborator_order_id",
                    "ordered_at",
                    ("price_paid_cents", "currency"),
                    "ad_label_requested",
                )
            },
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
    actions = ("check_indexation_action", "skip_checks_action", "resume_checks_action")

    class Media:
        js = (Js("seo/indexation.js"),)
        css: ClassVar[dict[str, tuple[Css, ...]]] = {
            "all": (Css("seo/indexation.css"), Css("seo/status-history.css"))
        }

    def get_urls(self) -> list[URLPattern]:
        own = [
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
        ]
        return own + super().get_urls()

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

    def panel_title(self, obj: Placement) -> str:
        # Статус — кнопками в самой форме, в заголовке он лишний.
        return f"{obj.site.domain} · {obj.product.name}"

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj is None:
            return [
                (name, {**options, "fields": _without(options["fields"], "status_history")})
                for name, options in fieldsets
            ]
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
        if obj is not None:
            context["panel_links"] = [
                ("Карточка площадки", reverse("admin:sites_site_card", args=[obj.site_id]), True)
            ]
        return super().render_change_form(request, context, add, change, form_url, obj)

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
        # Щелчок — панель с формой размещения, статус в ней первым (seo/panel.js);
        # с Ctrl и без скрипта — полная страница.
        url = reverse("admin:placements_placement_change", args=[obj.pk])
        return format_html(
            '<a href="{}" title="Сменить статус">{}</a>', url, obj.get_status_display()
        )

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


def _without(fields: Sequence[Any], name: str) -> tuple[Any, ...]:
    return tuple(field for field in fields if field != name)


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


def _task_states(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Состояние проверок по строкам журнала запусков (config/queue.py), одним запросом.

    Строки нет — задача ещё ждёт в очереди (или её отложило ограничение
    скорости): такой задачи в ответе нет. `errors` — неудачные попытки,
    после них будет повтор; `waiting` — пауза, например до полуночи по
    дневному лимиту.
    """
    if not task_ids:
        return {}
    runs = TaskRun.objects.filter(
        task_name="check_indexation",
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
    else:
        who = "кнопка" if result.get("manual") else "расписание"
    return (_when(check.checked_at), outcome, who, _when(check.next_check_at))
