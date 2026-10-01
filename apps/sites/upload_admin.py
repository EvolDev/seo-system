"""Экран «Загрузки»: файл → колонки → сводка до записи → разбор (E1-08, ADR-044).

Как выглядит — прототип `docs/prototypes/price-review.html`. Проверка и
запись идут задачами очереди: страница сводки опрашивает состояние
загрузки и показывает итог, когда задача закончила. Решения в разборе —
без перезагрузки страницы: JSON-запросы из `seo/upload-review.js`, тем же
приёмом, что кнопки индексации (ADR-042).
"""

import datetime as dt
import json
from typing import Any, ClassVar

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.views.decorators.http import require_GET, require_POST

from apps.sites.display import delta_html, price_html, seller_mark, writing_html
from apps.sites.models import (
    Seller,
    Upload,
    UploadItem,
    UploadKind,
    UploadStatus,
)
from apps.sites.offers import money
from apps.sites.rates import latest_rates
from apps.sites.tasks import upload_check, upload_write
from apps.sites.uploads import filters, review, service
from apps.sites.uploads.apply import Part
from apps.sites.uploads.columns import FIELD_LABELS, Confidence, Field
from config.admin import NoDeleteAdmin
from config.run_id import bind_run_id, new_run_id

CURRENCIES = ("USD", "EUR", "GBP", "PLN", "CZK", "UAH")
# Запрос со страницы без перезагрузки (seo/uploads.js) — ответ JSON.
AJAX_HEADER = "X-Seo-Ajax"
ALLOWED_SUFFIXES = (".xlsx", ".xlsm", ".csv", ".tsv", ".txt")


class UploadForm(forms.Form):
    kind = forms.ChoiceField(
        label="Что загружаем",
        choices=UploadKind.choices,
        initial=UploadKind.PRICE_LIST,
        widget=forms.RadioSelect,
    )
    seller = forms.ModelChoiceField(
        label="Продавец",
        queryset=Seller.objects.filter(is_collaborator=False).order_by("name"),
        required=False,
        empty_label="— выберите —",
    )
    new_seller = forms.CharField(label="или новый продавец", required=False, max_length=200)
    new_seller_currency = forms.ChoiceField(
        label="Валюта его прайсов",
        choices=[(c, c) for c in CURRENCIES],
        initial="USD",
        required=False,
    )
    prices_date = forms.DateField(
        label="Дата цен",
        initial=lambda: timezone.localdate(),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Цены станут снимком на эту дату. Повторная загрузка с той же датой "
        "обновит его, а не задвоит.",
    )
    file = forms.FileField(label="Файл", help_text="xlsx или csv.")

    def clean_file(self) -> Any:
        file = self.cleaned_data["file"]
        name = (file.name or "").lower()
        if not name.endswith(ALLOWED_SUFFIXES):
            raise forms.ValidationError("Нужен файл xlsx или csv.")
        return file

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("kind") == UploadKind.COLLABORATOR_CATALOG:
            data["seller"] = Seller.collaborator()
            return data
        name = " ".join((data.get("new_seller") or "").split())
        if data.get("seller") is None and not name:
            raise forms.ValidationError("Выберите продавца или впишите нового.")
        if data.get("seller") is None:
            existing = Seller.objects.filter(name__iexact=name).first()
            if existing is not None and existing.is_collaborator:
                raise forms.ValidationError("Collaborator загружается как «Каталог Collaborator».")
            data["seller_name"] = name
            data["seller_existing"] = existing
        return data


@admin.register(Upload)
class UploadAdmin(NoDeleteAdmin):
    """Список загрузок и их путь. Строку не правят: открыть — значит продолжить."""

    list_display = (
        "file_cell",
        "seller",
        "prices_date",
        "sites_cell",
        "progress_cell",
        "status_cell",
        "list_cell",
        "created_at",
    )
    list_display_links = None
    list_filter = ("kind", "status", "seller")
    ordering = ("-created_at", "-pk")
    list_per_page = 50

    class Media:
        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("seo/offers.css", "seo/uploads.css")}

    def get_queryset(self, request: HttpRequest) -> QuerySet[Upload]:
        need = Count("items", filter=Q(items__needs_decision=True))
        pending = Count(
            "items", filter=Q(items__needs_decision=True, items__price__reviewed_at__isnull=True)
        )
        queryset: QuerySet[Upload] = super().get_queryset(request)
        return queryset.select_related("seller", "site_list").annotate(need=need, pending=pending)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Штатной формы правки нет: загрузку продолжают по шагам.
        return False

    # ---------- Колонки списка ----------

    @admin.display(description="файл")
    def file_cell(self, obj: Upload) -> SafeString:
        return format_html(
            '<a href="{}">{}</a><div class="seo-sub">{}</div>',
            _step_url(obj),
            obj.file_name,
            obj.get_kind_display(),
        )

    @admin.display(description="площадок")
    def sites_cell(self, obj: Upload) -> str:
        data = obj.result or obj.summary or {}
        sites = data.get("sites")
        return f"{sites:,}".replace(",", " ") if isinstance(sites, int) else "—"

    @admin.display(description="разобрано")
    def progress_cell(self, obj: Upload) -> SafeString:
        need = getattr(obj, "need", 0)
        if obj.status != UploadStatus.DONE:
            return format_html('<span class="seo-flat">{}</span>', "—")
        if not need:
            return format_html('<span class="seo-chip seo-down">{}</span>', "решать нечего")
        done = need - getattr(obj, "pending", 0)
        chip = (
            '<span class="seo-chip seo-down">разобрано</span>'
            if done == need
            else '<span class="seo-chip seo-up">ждёт разбора</span>'
        )
        return format_html("{} из {} {}", done, need, SafeString(chip))

    @admin.display(description="состояние")
    def status_cell(self, obj: Upload) -> SafeString:
        if obj.status == UploadStatus.FAILED:
            return format_html(
                '<span class="seo-chip seo-up" title="{}">{}</span>',
                obj.error or "",
                obj.get_status_display(),
            )
        return format_html("{}", obj.get_status_display())

    @admin.display(description="рабочий список")
    def list_cell(self, obj: Upload) -> SafeString:
        if obj.site_list is None:
            return format_html("{}", "—")
        return format_html('<a href="{}">{}</a>', _sites_url(obj), obj.site_list.name)

    # ---------- Шаги ----------

    def get_urls(self) -> list[URLPattern]:
        view = self.admin_site.admin_view
        own = [
            path("<int:upload_id>/columns/", view(self.columns_view), name="sites_upload_columns"),
            path(
                "<int:upload_id>/summary/",
                view(require_GET(self.summary_view)),
                name="sites_upload_summary",
            ),
            path(
                "<int:upload_id>/write/",
                view(require_POST(self.write_view)),
                name="sites_upload_write",
            ),
            path(
                "<int:upload_id>/recheck/",
                view(require_POST(self.recheck_view)),
                name="sites_upload_recheck",
            ),
            path(
                "<int:upload_id>/count/",
                view(require_POST(self.count_view)),
                name="sites_upload_count",
            ),
            path(
                "<int:upload_id>/state/",
                view(require_GET(self.state_view)),
                name="sites_upload_state",
            ),
            path(
                "<int:upload_id>/review/",
                view(require_GET(self.review_view)),
                name="sites_upload_review",
            ),
            path(
                "<int:upload_id>/review/decide/",
                view(require_POST(self.decide_view)),
                name="sites_upload_decide",
            ),
            path(
                "<int:upload_id>/review/undo/",
                view(require_POST(self.undo_view)),
                name="sites_upload_undo",
            ),
        ]
        return own + super().get_urls()

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if not str(object_id).isdigit():
            raise Http404("Нет такой загрузки")
        upload = get_object_or_404(Upload, pk=object_id)
        return HttpResponseRedirect(_step_url(upload))

    def add_view(
        self, request: HttpRequest, form_url: str = "", extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        if not self.has_add_permission(request):
            return HttpResponse(status=403)
        form = UploadForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            seller = data["seller"] or data.get("seller_existing")
            if seller is None:
                seller = Seller.objects.create(
                    name=data["seller_name"], currency=data.get("new_seller_currency") or "USD"
                )
            created = service.create_upload(
                data["file"],
                kind=UploadKind(data["kind"]),
                seller=seller,
                prices_date=data["prices_date"],
                author=request.user,
            )
            upload = created.upload
            if created.duplicate:
                messages.info(request, "Этот файл с этой датой уже загружен — открыт его разбор.")
                return HttpResponseRedirect(_step_url(upload))
            if upload.status == UploadStatus.NEW and not service.needs_questions(upload):
                # Каталог и знакомый прайс — без вопросов, сразу проверка.
                _start_check(upload)
            return HttpResponseRedirect(_step_url(upload))
        context = {
            **self._context(request, "Новая загрузка", step=1),
            "form": form,
        }
        return TemplateResponse(request, "admin/sites/upload/new.html", context)

    def columns_view(self, request: HttpRequest, upload_id: int) -> HttpResponse:
        upload = get_object_or_404(Upload.objects.select_related("seller"), pk=upload_id)
        if not self.has_add_permission(request):
            return HttpResponse(status=403)
        if upload.kind == UploadKind.COLLABORATOR_CATALOG or upload.status not in (
            UploadStatus.NEW,
            UploadStatus.CHECKED,
            UploadStatus.FAILED,
        ):
            return HttpResponseRedirect(_step_url(upload))
        errors: list[str] = []
        if request.method == "POST":
            header_row = _int(request.POST.get("header_row"))
            if header_row and header_row != upload.header_row:
                service.prepare(upload, header_row=header_row)
                messages.info(
                    request, f"Заголовки взяты из строки {header_row} — проверьте колонки."
                )
                return HttpResponseRedirect(_url("columns", upload))
            mapping = {
                str(column["key"]): request.POST.get(f"field:{column['key']}", Field.EXTRA.value)
                for column in upload.columns or []
            }
            errors = service.confirm_mapping(
                upload, mapping, request.POST.get("currency") or upload.currency or "EUR"
            )
            if not errors:
                _start_check(upload)
                return HttpResponseRedirect(_url("summary", upload))
        columns = upload.columns or []
        context = {
            **self._context(request, "Колонки файла", step=2, upload=upload),
            "errors": errors,
            "asked": [c for c in columns if c.get("ask")],
            "known": [c for c in columns if not c.get("ask") and c.get("filled")],
            "empty": [c for c in columns if not c.get("filled")],
            "fields": [(f.value, label) for f, label in FIELD_LABELS.items()],
            "confidence": {c.value: _confidence_class(c) for c in Confidence},
            "currencies": sorted({*CURRENCIES, upload.currency or "EUR"}),
            "rates": latest_rates(),
        }
        return TemplateResponse(request, "admin/sites/upload/columns.html", context)

    def summary_view(self, request: HttpRequest, upload_id: int) -> HttpResponse:
        upload = get_object_or_404(Upload.objects.select_related("seller"), pk=upload_id)
        catalog_kind = upload.kind == UploadKind.COLLABORATOR_CATALOG
        if upload.status == UploadStatus.DONE and not catalog_kind:
            return HttpResponseRedirect(_url("review", upload))
        if upload.status == UploadStatus.NEW:
            return HttpResponseRedirect(_step_url(upload))
        busy = upload.status in (UploadStatus.CHECKING, UploadStatus.WRITING)
        context = {
            **self._context(request, "Сводка до записи", step=3, upload=upload),
            "summary": upload.summary or {},
            "issues": service.issues(upload),
            "busy": busy,
            "list_name": f"{upload.seller.name} · {upload.prices_date:%d.%m.%Y}",
        }
        if catalog_kind and not busy and upload.status != UploadStatus.FAILED:
            context.update(_catalog_context(upload))
            context["title"] = "Каталог Collaborator: что записать"
            return TemplateResponse(request, "admin/sites/upload/catalog.html", context)
        return TemplateResponse(request, "admin/sites/upload/summary.html", context)

    def write_view(self, request: HttpRequest, upload_id: int) -> HttpResponse:
        """Запуск записи. Из страницы — JSON (без перезагрузки), без скрипта — переход."""
        upload = get_object_or_404(Upload, pk=upload_id)
        if not self.has_add_permission(request):
            return _answer(request, upload, error="Нет прав на загрузку.", status=403)
        action = request.POST.get("action") or service.Action.ALL
        catalog_kind = upload.kind == UploadKind.COLLABORATOR_CATALOG
        allowed = (
            (UploadStatus.CHECKED, UploadStatus.DONE) if catalog_kind else (UploadStatus.CHECKED,)
        )
        if action not in service.Action.__members__.values() or upload.status not in allowed:
            return _answer(request, upload, error="Сейчас запустить нельзя — обновите страницу.")
        if catalog_kind and action == service.Action.ALL:
            # У каталога — только кнопки «Обновить в базе» и «Добавить новые».
            return _answer(
                request, upload, error="Выберите «Обновить в базе» или «Добавить новые»."
            )
        parts = [p for p in request.POST.getlist("parts") if p in Part.__members__.values()]
        if not catalog_kind:
            parts = [p.value for p in Part]
        if action == service.Action.KNOWN and not parts:
            return _answer(
                request, upload, error="Отметьте, что обновить: цены, метрики или описание."
            )
        try:
            spec = filters.parse_spec(json.loads(request.POST.get("filters") or "{}"))
        except ValueError:
            spec = {}
        service.start(upload, UploadStatus.WRITING)
        with bind_run_id(upload.run_id or new_run_id()):
            upload_write.delay(upload.pk, action, parts, spec)
        return _answer(request, upload)

    def recheck_view(self, request: HttpRequest, upload_id: int) -> HttpResponse:
        """«Пересчитать сводку»: база могла измениться с проверки — посчитать заново."""
        upload = get_object_or_404(Upload, pk=upload_id)
        if not self.has_add_permission(request):
            return HttpResponse(status=403)
        if upload.status in (UploadStatus.CHECKED, UploadStatus.DONE):
            _start_check(upload)
        return _answer(request, upload)

    def count_view(self, request: HttpRequest, upload_id: int) -> JsonResponse:
        """Живой счётчик «будет добавлено» для фильтров каталога."""
        upload = get_object_or_404(Upload, pk=upload_id)
        try:
            spec = filters.parse_spec(json.loads(request.body).get("filters"))
        except (ValueError, AttributeError):
            return JsonResponse({"error": "Неверный запрос."}, status=400)
        count = filters.count_new(upload, spec)
        return JsonResponse(
            {"passed": count.passed, "total": count.total, "text": filters.describe(spec)}
        )

    def state_view(self, request: HttpRequest, upload_id: int) -> JsonResponse:
        upload = get_object_or_404(Upload, pk=upload_id)
        busy = upload.status in (UploadStatus.CHECKING, UploadStatus.WRITING)
        return JsonResponse(
            {
                "status": upload.status,
                "label": upload.get_status_display(),
                "busy": busy,
                "error": upload.error,
                "next": None if busy else _step_url(upload),
            }
        )

    def review_view(self, request: HttpRequest, upload_id: int) -> HttpResponse:
        upload = get_object_or_404(
            Upload.objects.select_related("seller", "site_list"), pk=upload_id
        )
        if upload.status != UploadStatus.DONE:
            return HttpResponseRedirect(_step_url(upload))
        all_tabs = review.tabs(upload)
        tab = request.GET.get("tab") or review.first_tab(upload, all_tabs)
        if tab not in {t.key for t in all_tabs}:
            tab = all_tabs[0].key
        sort = request.GET.get("sort") or "gain"
        if sort not in review.SORTS:
            sort = "gain"
        hide_done = request.GET.get("all") != "1"
        done, need = review.progress(upload)
        context = {
            **self._context(
                request,
                f"Разбор: {upload.seller.name} · {upload.prices_date:%d.%m.%Y}",
                step=4,
                upload=upload,
            ),
            "tabs": all_tabs,
            "tab": tab,
            "tab_note": review.TAB_NOTES.get(tab, ""),
            "decidable": tab in review.DECIDE,
            "sort": sort,
            # Без разницы с рабочей ценой «выгода» — порядок строк файла.
            "sorts": review.SORTS if tab in review.GAIN_GROUPS else review.FILE_SORTS,
            "hide_done": hide_done,
            "done": done,
            "need": need,
            "percent": round(done * 100 / need) if need else 100,
            "sites_url": _sites_url(upload),
            "data": service.issues(upload),
            "actions_url": _url("summary", upload)
            if upload.kind == UploadKind.COLLABORATOR_CATALOG
            else None,
            "can_decide": request.user.has_perm("sites.change_site"),
        }
        if tab == review.ISSUES:
            context["rows"] = []
        else:
            current, rows = review.page(
                upload, tab, sort=sort, hide_done=hide_done, number=_int(request.GET.get("p")) or 1
            )
            context["page"] = current
            context["rows"] = [_row_context(row) for row in rows]
        return TemplateResponse(request, "admin/sites/upload/review.html", context)

    def decide_view(self, request: HttpRequest, upload_id: int) -> JsonResponse:
        upload = get_object_or_404(Upload, pk=upload_id)
        if not request.user.has_perm("sites.change_site"):
            return JsonResponse({"error": "Нет прав менять цены площадок."}, status=403)
        try:
            payload = json.loads(request.body)
            action = payload["action"]
            ids = [int(pk) for pk in payload["items"]]
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": "Неверный запрос."}, status=400)
        if action not in ("fix", "keep", *review.REMOVE_ACTIONS):
            return JsonResponse({"error": "Неизвестное действие."}, status=400)
        undo, problems = review.decide(upload, ids, action, author=request.user)
        if action in review.REMOVE_ACTIONS:
            # Публикация и вставка одной площадки — разные строки: уходят обе.
            ids = _site_items(upload, ids)
        return JsonResponse({**_after_decision(upload, ids, undo), "problems": problems})

    def undo_view(self, request: HttpRequest, upload_id: int) -> JsonResponse:
        upload = get_object_or_404(Upload, pk=upload_id)
        if not request.user.has_perm("sites.change_site"):
            return JsonResponse({"error": "Нет прав менять цены площадок."}, status=403)
        try:
            entries = json.loads(request.body)["undo"]
            items = [int(entry["item"]) for entry in entries]
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": "Неверный запрос."}, status=400)
        sites = set(
            UploadItem.objects.filter(upload=upload, pk__in=items).values_list("site_id", flat=True)
        )
        review.undo([e for e in entries if int(e["site"]) in sites], author=request.user)
        return JsonResponse(_after_decision(upload, _site_items(upload, items), []))

    def _context(
        self, request: HttpRequest, title: str, *, step: int, upload: Upload | None = None
    ) -> dict[str, Any]:
        return {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.model._meta,
            "upload": upload,
            "step": step,
            "steps": ["Файл", "Колонки", "Сводка до записи", "Разбор"],
        }


def _answer(
    request: HttpRequest, upload: Upload, *, error: str | None = None, status: int = 200
) -> HttpResponse:
    """Ответ кнопки: странице — JSON с адресом опроса, без скрипта — переход и сообщение."""
    if request.headers.get(AJAX_HEADER) == "1":
        if error:
            return JsonResponse({"error": error}, status=status if status != 200 else 400)
        return JsonResponse({"state_url": _url("state", upload)})
    if error:
        messages.warning(request, error)
    return HttpResponseRedirect(_url("summary", upload))


def _catalog_context(upload: Upload) -> dict[str, Any]:
    """Кнопки каталога: что уже в базе, сколько новых, фильтры с облачками, история нажатий."""
    count = filters.count_new(upload, {})
    previous = Upload.objects.filter(
        kind=UploadKind.COLLABORATOR_CATALOG, result__isnull=False
    ).order_by("-created_at", "-pk")[:10]
    # Своя загрузка — первой: её фильтры свежее всех.
    ordered = [upload, *[u for u in previous if u.pk != upload.pk]]
    chips = filters.suggestions(ordered)
    choices = filters.choices(upload)
    fields = [
        {
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "unit": field.unit,
            "chips": chips[field.key],
            "choices": choices.get(field.key, []),
        }
        for field in filters.FIELDS
    ]
    runs = []
    for run in reversed((upload.result or {}).get("runs", [])):
        action = service.Action(run.get("action", "all"))
        runs.append(
            {
                "label": action.label,
                "at": dt.datetime.fromisoformat(run["at"]) if run.get("at") else None,
                "parts": ", ".join(Part(p).short for p in run.get("parts", [])),
                "filter_text": run.get("filter_text") or "",
                "sites": run.get("sites", 0),
                "created": (run.get("counts") or {}).get("sites_created", 0),
                "decide": (run.get("counts") or {}).get("needs_decision", 0),
            }
        )
    return {
        "new_total": count.total,
        "known_total": (upload.summary or {}).get("known", 0),
        "fields": fields,
        "fields_data": [
            {k: f[k] for k in ("key", "kind", "unit", "chips", "choices")} for f in fields
        ],
        "parts": [(p.value, p.label) for p in Part],
        "runs": runs,
    }


def _start_check(upload: Upload) -> None:
    upload.run_id = new_run_id()
    upload.save(update_fields=["run_id"])
    service.start(upload, UploadStatus.CHECKING)
    with bind_run_id(upload.run_id):
        upload_check.delay(upload.pk)


def _site_items(upload: Upload, ids: list[int]) -> list[int]:
    """Все строки загрузки у площадок этих строк."""
    sites = UploadItem.objects.filter(upload=upload, pk__in=ids).values("site_id")
    return list(
        UploadItem.objects.filter(upload=upload, site_id__in=sites).values_list("pk", flat=True)
    )


def _after_decision(upload: Upload, ids: list[int], undo: list[dict[str, Any]]) -> dict[str, Any]:
    done, need = review.progress(upload)
    return {
        "rows": {str(pk): state for pk, state in review.row_states(ids).items()},
        "undo": undo,
        "done": done,
        "need": need,
        "tabs": {t.key: t.badge for t in review.tabs(upload)},
    }


def _row_context(row: review.Row) -> dict[str, Any]:
    item = row.item
    ref = item.ref_price
    return {
        "row": row,
        "item": item,
        "domain": item.site.domain,
        "card_url": card_url(item.site_id),
        "service": item.price.get_placement_type_display(),
        "price_html": price_html(row.price),
        "ref_html": price_html(row.ref) if row.ref is not None else None,
        "ref_seller": ref.seller.name if ref is not None else "",
        "ref_service": ref.get_placement_type_display() if ref is not None else "",
        "writing_html": writing_html(ref.writing_cents, ref.currency) if ref is not None else "",
        "gray_text": money(item.price.gray_cents, item.price.currency),
        "delta_html": delta_html(row.change) if row.ref is not None else None,
        "metrics_mark": seller_mark(row.metrics_seller) if row.metrics_seller else "",
    }


def _url(step: str, upload: Upload) -> str:
    return reverse(f"admin:sites_upload_{step}", args=[upload.pk])


def _step_url(upload: Upload) -> str:
    """Куда ведёт загрузка: на шаг, где она сейчас. Каталог — на кнопки записи."""
    if upload.status == UploadStatus.DONE and upload.kind == UploadKind.PRICE_LIST:
        return _url("review", upload)
    if upload.status == UploadStatus.NEW and upload.kind == UploadKind.PRICE_LIST:
        return _url("columns", upload)
    return _url("summary", upload)


def _sites_url(upload: Upload) -> str:
    base = reverse("admin:sites_productsitelatest_changelist")
    return f"{base}?list={upload.site_list_id}" if upload.site_list_id else base


def _confidence_class(confidence: Confidence) -> str:
    return {
        Confidence.SURE: "seo-down",
        Confidence.LIKELY: "seo-up",
        Confidence.REMEMBERED: "seo-down",
        Confidence.CHOSEN: "seo-info",
    }.get(confidence, "")


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def card_url(site_id: int) -> str:
    """Карточка площадки — окном по ссылке с data-site-card (seo/site-card.js)."""
    return reverse("admin:sites_site_card", args=[site_id])
