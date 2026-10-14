"""Конструктор выгрузки в админке. Шаблоны личные; образец временный и не
импортирует записи в базу размещений.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from django.contrib.admin import ModelAdmin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.db.models.functions import Lower
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.placements import export_designer as design
from apps.placements.models import Placement
from apps.sites.uploads.files import FileError, read_sheets, show
from apps.workspace.models import ExportProfile
from config import export

TEMPLATE_TTL = 3600
MAX_FILE_BYTES = 20 * 1024 * 1024


def _user_id(request: HttpRequest) -> int:
    pk = request.user.pk
    if pk is None:
        raise PermissionDenied
    return pk


def _profile_id(value: object) -> int:
    if type(value) is not int:
        raise design.DesignError("Неверный номер шаблона.")
    return value


def _profiles(user_id: int) -> list[dict[str, Any]]:
    return [
        {"id": p.pk, "name": p.name, "revision": p.revision, "config": p.config}
        for p in ExportProfile.objects.filter(
            user_id=user_id, screen=design.SCREEN, deleted_at__isnull=True
        )
        .exclude(name="")
        .order_by(Lower("name"), "pk")
    ]


def _template(request: HttpRequest, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    value = cache.get(f"export-template:{_user_id(request)}:{token}")
    if not isinstance(value, dict):
        raise design.DesignError("Образец истёк. Загрузите файл ещё раз.")
    return value


def _queryset(model_admin: ModelAdmin[Placement], request: HttpRequest) -> QuerySet[Placement]:
    queryset: QuerySet[Placement] = model_admin.get_changelist_instance(request).queryset
    ids = request.POST.getlist("ids")
    if ids and request.POST.get("select_across") != "1":
        if any(not value.isdigit() for value in ids):
            raise design.DesignError("Неверные номера отмеченных размещений.")
        queryset = queryset.filter(pk__in=[int(value) for value in ids])
    return queryset


def _import(request: HttpRequest, registry: dict[str, design.Field]) -> JsonResponse:
    upload = request.FILES.get("file")
    if upload is None or upload.size is None or upload.size > MAX_FILE_BYTES:
        raise design.DesignError("Выберите CSV или XLSX размером до 20 МБ.")
    suffix = Path(upload.name or "").suffix.lower()
    if suffix not in (".csv", ".tsv", ".xlsx"):
        raise design.DesignError("Нужен CSV, TSV или XLSX.")
    with tempfile.TemporaryDirectory(prefix="seo-export-") as directory:
        path = Path(directory) / f"sample{suffix}"
        with path.open("wb") as target:
            for chunk in upload.chunks():
                target.write(chunk)
        sheets = read_sheets(path)
    if not sheets:
        raise design.DesignError("В файле нет данных.")
    sheet_index = int(request.POST.get("sheet", "0"))
    header_row = int(request.POST.get("header_row", "1"))
    if not 0 <= sheet_index < len(sheets):
        raise design.DesignError("Выберите лист книги.")
    title, source = sheets[sheet_index]
    if not 1 <= header_row <= len(source):
        raise design.DesignError("В файле нет указанной строки заголовков.")
    if len(source) - header_row > design.MAX_ROWS:
        raise design.DesignError(f"В образце допускается до {design.MAX_ROWS} строк.")
    width = max(len(row) for row in source[header_row - 1 :])
    if not 1 <= width <= design.MAX_COLUMNS:
        raise design.DesignError(f"В образце допускается до {design.MAX_COLUMNS} колонок.")
    headers = [
        show(source[header_row - 1][i]) if i < len(source[header_row - 1]) else ""
        for i in range(width)
    ]
    template: dict[str, Any] = {
        "name": upload.name,
        "sheet": title,
        "headers": headers,
        "rows": [
            {
                "line": line,
                "cells": [show(cells[i]) if i < len(cells) else "" for i in range(width)],
            }
            for line, cells in enumerate(source[header_row:], header_row + 1)
        ],
    }
    token = uuid.uuid4().hex
    cache.set(f"export-template:{_user_id(request)}:{token}", template, TEMPLATE_TTL)
    return JsonResponse(
        {
            "token": token,
            "name": upload.name,
            "count": len(template["rows"]),
            "sheets": [name for name, _ in sheets],
            "config": design.suggest(headers, registry),
        }
    )


def _save_profile(
    request: HttpRequest, payload: dict[str, Any], registry: dict[str, design.Field]
) -> JsonResponse:
    config = design.validate(payload.get("config"), registry)
    action = request.POST["action"]
    name = "" if action == "draft" else " ".join(str(payload.get("name", "")).split())
    if action != "draft" and not 1 <= len(name) <= 80:
        raise design.DesignError("Назовите шаблон: до 80 символов.")
    with transaction.atomic():
        if action == "draft":
            profile, _ = ExportProfile.objects.update_or_create(
                user_id=_user_id(request),
                screen=design.SCREEN,
                name="",
                deleted_at=None,
                defaults={"config": config, "updated_at": timezone.now()},
            )
        elif payload.get("id") is not None:
            existing = (
                ExportProfile.objects.select_for_update()
                .filter(
                    pk=payload["id"],
                    user_id=_user_id(request),
                    screen=design.SCREEN,
                    deleted_at__isnull=True,
                )
                .exclude(name="")
                .first()
            )
            if existing is None:
                raise PermissionDenied
            profile = existing
            if profile.revision != payload.get("revision"):
                return JsonResponse(
                    {"error": "Шаблон изменён в другой вкладке. Обновите список."}, status=409
                )
            profile.name, profile.config = name, config
            profile.revision += 1
            profile.updated_at = timezone.now()
            profile.save(update_fields=["name", "config", "revision", "updated_at"])
        else:
            profile = ExportProfile.objects.create(
                user_id=_user_id(request), screen=design.SCREEN, name=name, config=config
            )
    return JsonResponse(
        {"id": profile.pk, "revision": profile.revision, "profiles": _profiles(_user_id(request))}
    )


def view(model_admin: ModelAdmin[Placement], request: HttpRequest) -> HttpResponse:
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    try:
        return _view(model_admin, request)
    except (design.DesignError, FileError, ValueError, TypeError) as error:
        return JsonResponse({"error": str(error) or "Неверные настройки выгрузки."}, status=400)
    except IntegrityError:
        return JsonResponse({"error": "Шаблон с таким названием уже существует."}, status=409)


def _view(model_admin: ModelAdmin[Placement], request: HttpRequest) -> HttpResponse:
    registry = design.fields()
    action = request.POST.get("action", "open")
    payload = json.loads(request.POST.get("payload", "{}"))
    if not isinstance(payload, dict):
        raise design.DesignError("Неверные настройки выгрузки.")
    if request.method == "POST":
        if action == "import":
            return _import(request, registry)
        if action in ("draft", "save"):
            return _save_profile(request, payload, registry)
        if action in ("delete", "restore"):
            with transaction.atomic():
                profile = (
                    ExportProfile.objects.select_for_update()
                    .filter(
                        pk=_profile_id(payload.get("id")),
                        user_id=_user_id(request),
                        screen=design.SCREEN,
                    )
                    .exclude(name="")
                    .first()
                )
                if profile is None:
                    raise PermissionDenied
                if profile.revision != payload.get("revision"):
                    return JsonResponse({"error": "Шаблон изменён. Обновите список."}, status=409)
                profile.deleted_at = timezone.now() if action == "delete" else None
                profile.revision += 1
                profile.save(update_fields=["deleted_at", "revision"])
            return JsonResponse(
                {
                    "id": profile.pk,
                    "revision": profile.revision,
                    "profiles": _profiles(_user_id(request)),
                }
            )
        if action in ("preview", "download", "copy"):
            config = design.validate(payload.get("config"), registry)
            template = _template(request, str(payload.get("token", "")))
            records = design.dataset(_queryset(model_admin, request), registry)
            decisions = payload.get("decisions", {})
            if not isinstance(decisions, dict):
                raise design.DesignError("Неверные решения для строк образца.")
            plan = design.build_plan(records, config, registry, template, decisions)
            table = [
                [design.display(c, v, config) for c, v in zip(plan.sheet.columns, row, strict=True)]
                for row in plan.sheet.rows
            ]
            if action != "preview":
                if plan.issues:
                    return JsonResponse(
                        {
                            "error": "Разберите все спорные строки перед выгрузкой.",
                            "issues": plan.issues,
                        },
                        status=409,
                    )
                if payload.get("digest") != plan.digest:
                    return JsonResponse(
                        {"error": "Данные изменились. Обновите предпросмотр."}, status=409
                    )
            if action == "download":
                fmt = payload.get("format")
                if fmt == "xlsx":
                    return export.response([plan.sheet], "Размещения", export.Format.XLSX)
                if fmt == "csv":
                    return export.attachment(
                        design.csv_data(plan.sheet, config),
                        "Размещения.csv",
                        export.CONTENT_TYPES[export.Format.CSV],
                    )
                raise design.DesignError("Выберите Excel XLSX или CSV.")
            if action == "copy":
                start, stop = payload.get("start", 0), payload.get("stop", len(plan.sheet.columns))
                if (
                    type(start) is not int
                    or type(stop) is not int
                    or not 0 <= start < stop <= len(plan.sheet.columns)
                ):
                    raise design.DesignError(
                        "Выберите непрерывный диапазон колонок для копирования."
                    )
                return JsonResponse(
                    {"rows": [row[start:stop] for row in table], "count": len(table)}
                )
            first = records[0] if records else None
            examples = {
                key: design.display(field.column, first["values"].get(key), config) if first else ""
                for key, field in registry.items()
            }
            return JsonResponse(
                {
                    "headers": [c.title for c in plan.sheet.columns],
                    "rows": table[:5],
                    "lines": plan.row_numbers[:5],
                    "count": len(table),
                    "digest": plan.digest,
                    "issues": plan.issues,
                    "examples": examples,
                    "domain": first["site__domain"] if first else "",
                }
            )
        if action != "open":
            raise design.DesignError("Неизвестное действие.")
    queryset = _queryset(model_admin, request)
    draft = ExportProfile.objects.filter(
        user_id=_user_id(request), screen=design.SCREEN, name="", deleted_at__isnull=True
    ).first()
    bootstrap = {
        "fields": [f.as_json() for f in registry.values()],
        "initial": design.initial(registry),
        "config": draft.config if draft else design.initial(registry),
        "profiles": _profiles(_user_id(request)),
        "count": queryset.count(),
        "ids": request.POST.getlist("ids"),
        "select_across": request.POST.get("select_across", "0"),
    }
    back = reverse("admin:placements_placement_changelist")
    if request.GET:
        back += "?" + request.GET.urlencode()
    return render(
        request,
        "admin/placements/placement/export_designer.html",
        {
            **model_admin.admin_site.each_context(request),
            "bootstrap": bootstrap,
            "back_url": back,
            "title": "Конструктор выгрузки",
            "opts": model_admin.model._meta,
        },
    )
