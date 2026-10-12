"""Копирование и Excel анкоров: тот же отбор, все страницы списка."""

from typing import Any

from django.contrib.admin import ModelAdmin
from django.contrib.admin.options import IncorrectLookupParameters
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone

from apps.keywords.models import KeywordCoverage
from config import export
from config.admin import PER_PAGE_PARAM


class AnchorTransferChangeList(export.ExportChangeList):
    """Размер страницы относится к экрану, а не к фильтрам выгрузки."""

    def get_filters_params(self, params: Any = None) -> Any:
        found = super().get_filters_params(params)
        found.pop(PER_PAGE_PARAM, None)
        return found


def _queryset(model_admin: "ModelAdmin[Any]", request: HttpRequest) -> QuerySet[KeywordCoverage]:
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    queryset: QuerySet[KeywordCoverage] = model_admin.get_changelist_instance(request).queryset
    return queryset


def copy_view(model_admin: "ModelAdmin[Any]", request: HttpRequest) -> HttpResponse:
    try:
        queryset = _queryset(model_admin, request)
    except IncorrectLookupParameters:
        return HttpResponseBadRequest("Не удалось применить фильтры списка.")
    if request.POST.get("select_across") != "1":
        ids = request.POST.getlist("ids")
        if not ids or any(not value.isdigit() for value in ids):
            return HttpResponseBadRequest("Отметьте анкоры для копирования.")
        queryset = queryset.filter(pk__in=[int(value) for value in ids])
    texts = list(queryset.values_list("keyword", flat=True))
    return JsonResponse({"text": "\n".join(texts), "count": len(texts)})


def excel_view(model_admin: "ModelAdmin[Any]", request: HttpRequest) -> HttpResponse:
    try:
        queryset = _queryset(model_admin, request)
    except IncorrectLookupParameters:
        return HttpResponseBadRequest("Не удалось применить фильтры списка.")
    sheet = export.Sheet(
        "Анкоры",
        (
            export.Column("Keyword", width=40),
            export.Column("URL", width=55),
            export.Column("Volume", export.Kind.INT),
            export.Column("Global Volume", export.Kind.INT, width=18),
        ),
        queryset.values_list("keyword", "target_url", "volume", "global_volume").iterator(),
    )
    return export.response([sheet], f"Анкоры {timezone.localdate():%Y-%m-%d}", export.Format.XLSX)
