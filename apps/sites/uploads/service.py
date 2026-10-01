"""Путь загрузки целиком: файл → разметка → проверка → запись (ADR-044).

Экран загрузки и задачи очереди зовут только эти функции. Проверка и запись
идут в фоне: каталог Collaborator — 45 000 строк. Запись строит план заново
внутри своей транзакции — по свежему состоянию базы, а не по сводке, которую
человек видел минуту назад.
"""

import datetime as dt
import hashlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.content.domain_settings import offer_recheck
from apps.sites.models import Seller, Upload, UploadKind, UploadStatus
from apps.sites.rates import latest_rates
from apps.sites.uploads import catalog, filters
from apps.sites.uploads.apply import ALL_PARTS, Part, Writer
from apps.sites.uploads.columns import (
    Confidence,
    Field,
    build_mapping,
    detect_currency,
    looks_like_header,
    validate_mapping,
)
from apps.sites.uploads.files import Column, FileError, Table, read_table
from apps.sites.uploads.plan import Plan, build_plan
from apps.sites.uploads.records import Parsed, parse_price_list

logger = logging.getLogger(__name__)


class UploadedFile(Protocol):
    """Файл из формы Django: имя и чтение кусками — большой файл не держим в памяти."""

    name: str | None

    def chunks(self, chunk_size: int | None = None) -> Iterable[bytes]: ...


@dataclass(frozen=True)
class Created:
    upload: Upload
    duplicate: bool  # этот файл с этой датой уже записан — открыть его разбор


def create_upload(
    file: UploadedFile,
    *,
    kind: UploadKind,
    seller: Seller,
    prices_date: dt.date,
    author: Any = None,
) -> Created:
    """Сохраняет файл и читает его колонки. Тот же файл с той же датой — прежняя загрузка."""
    name = Path(file.name or "file").name
    relative = Path(f"{prices_date:%Y/%m}") / f"{uuid4().hex}_{name}"
    target = Path(settings.UPLOADS_DIR) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with target.open("wb") as out:
        for chunk in file.chunks():
            digest.update(chunk)
            out.write(chunk)
    sha = digest.hexdigest()
    done = Upload.objects.filter(
        kind=kind, seller=seller, prices_date=prices_date, file_sha256=sha, status=UploadStatus.DONE
    ).first()
    if done is not None:
        target.unlink()
        return Created(done, duplicate=True)
    upload = Upload.objects.create(
        kind=kind,
        seller=seller,
        prices_date=prices_date,
        file_name=name,
        file_path=str(relative),
        file_sha256=sha,
        author=author,
    )
    prepare(upload)
    return Created(upload, duplicate=False)


def file_path(upload: Upload) -> Path:
    return Path(settings.UPLOADS_DIR) / upload.file_path


def prepare(upload: Upload, *, header_row: int | None = None) -> None:
    """Читает колонки файла и предлагает разметку. Не прочитался — ошибка у загрузки."""
    try:
        table = _table(upload, header_row=header_row)
    except FileError as error:
        _fail(upload, str(error))
        return
    upload.header_row = table.header_row
    upload.error = None
    upload.status = UploadStatus.NEW
    if upload.kind == UploadKind.COLLABORATOR_CATALOG:
        missing = catalog.missing_columns(table)
        upload.columns = [_column_json(c) for c in table.columns]
        upload.mapping = None
        upload.currency = "EUR"
        if missing:
            _fail(
                upload,
                "Не похоже на выгрузку каталога Collaborator: нет колонок "
                + ", ".join(f"«{name}»" for name in missing)
                + ". Нужна выгрузка в английском интерфейсе.",
            )
            return
    else:
        guesses, questions = build_mapping(table.columns, upload.seller.column_map)
        mapping = {key: guess.field for key, guess in guesses.items()}
        upload.columns = [
            _column_json(c, guesses[c.key].field, guesses[c.key].confidence, guesses[c.key].hint)
            | {"ask": c.key in questions}
            for c in table.columns
        ]
        upload.mapping = {key: field.value for key, field in mapping.items()}
        upload.currency, _ = detect_currency(table.columns, mapping, upload.seller.currency)
    upload.summary = None
    upload.save()


def needs_questions(upload: Upload) -> bool:
    """Есть колонки, про которые надо спросить, или разметка с ошибкой."""
    if upload.kind == UploadKind.COLLABORATOR_CATALOG:
        return False
    if any(column.get("ask") for column in upload.columns or []):
        return True
    return bool(mapping_errors(upload, _mapping(upload)))


def mapping_errors(upload: Upload, mapping: Mapping[str, Field]) -> list[str]:
    columns = [_column(c) for c in upload.columns or []]
    return validate_mapping(mapping, columns)


def confirm_mapping(upload: Upload, mapping: Mapping[str, str], currency: str) -> list[str]:
    """Разметка от человека: проверка и память у продавца. Ошибки — человеческим языком."""
    try:
        fields = {key: Field(value) for key, value in mapping.items()}
    except ValueError:
        return ["Неизвестное поле в разметке — обновите страницу."]
    errors = mapping_errors(upload, fields)
    currency = currency.strip().upper()
    if currency not in latest_rates():
        errors.append(
            f"Для валюты {currency} нет курса ЕЦБ — с рабочими ценами не сравнить. "
            "Выберите другую валюту или обновите курсы."
        )
    if errors:
        return errors
    upload.mapping = {key: field.value for key, field in fields.items()}
    upload.currency = currency
    upload.columns = [
        column | {"field": upload.mapping.get(column["key"], Field.EXTRA.value), "ask": False}
        for column in upload.columns or []
    ]
    upload.save(update_fields=["mapping", "currency", "columns"])
    # Разметка запоминается у продавца: следующий файл с этими колонками — без вопросов.
    seller = upload.seller
    seller.column_map = {**(seller.column_map or {}), **upload.mapping}
    seller.save(update_fields=["column_map"])
    return []


def parse(upload: Upload) -> tuple[Table, Parsed, list[str]]:
    """Файл → записи по разметке загрузки. Третье — незнакомые значения каталога."""
    table = _table(upload, header_row=upload.header_row)
    if upload.kind == UploadKind.COLLABORATOR_CATALOG:
        unknown = catalog.Unknown()
        return table, catalog.parse_catalog(table, unknown), unknown.lines()
    return table, parse_price_list(table, _mapping(upload), upload.currency or "EUR"), []


def plan_for(upload: Upload, parsed: Parsed) -> Plan:
    return build_plan(
        parsed,
        seller_id=upload.seller_id,
        currency=upload.currency or "EUR",
        prices_date=upload.prices_date,
        rates=latest_rates(),
        recheck_pct=offer_recheck().min_change_pct,
    )


def check(upload_id: int) -> None:
    """Сводка до записи: план без записи. Тело задачи очереди `upload_check`."""
    upload = Upload.objects.select_related("seller").get(pk=upload_id)
    if upload.status != UploadStatus.CHECKING:
        return
    try:
        table, parsed, unknown = parse(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    plan = plan_for(upload, parsed)
    upload.summary = plan.summary(
        rows=len(table.rows), blank_rows=table.blank_rows, unknown=unknown
    )
    # Пересчёт сводки у загрузки, из которой уже записывали, разбор не закрывает.
    written = bool((upload.result or {}).get("runs"))
    upload.status = UploadStatus.DONE if written else UploadStatus.CHECKED
    upload.error = None
    upload.save(update_fields=["summary", "status", "error"])
    if upload.kind == UploadKind.COLLABORATOR_CATALOG:
        # Строки для фильтров «Добавить новые» — сразу в кеш: счётчик на экране
        # не будет ждать повторного разбора 45 000 строк.
        filters.cached_rows(upload, table)
    logger.info(
        "загрузка проверена", extra={"upload_id": upload.pk, "summary": _short(upload.summary)}
    )


class Action(StrEnum):
    """Что записать: всё (прайс продавца) или одной из кнопок каталога (ADR-044)."""

    ALL = "all"
    KNOWN = "known"  # «Обновить в базе» — площадки, которые уже есть
    NEW = "new"  # «Добавить новые» — только новые для базы, по фильтру

    @property
    def label(self) -> str:
        return {"all": "Записать в базу", "known": "Обновить в базе", "new": "Добавить новые"}[
            self.value
        ]


def write(
    upload_id: int,
    action: str = Action.ALL,
    parts: Iterable[str] | None = None,
    spec: Mapping[str, Any] | None = None,
) -> None:
    """Запись в одной транзакции. Тело задачи очереди `upload_write`.

    Задача идемпотентна: состояние «Записывается» ставит кнопка, повтор
    после записи ничего не делает, прерванная запись откатилась целиком и
    начнётся с нуля. У каталога каждое нажатие — строка в истории
    загрузки (`result["runs"]`), сводка после записи пересчитывается.
    """
    with transaction.atomic():
        # select_for_update — строка загрузки заблокирована до конца транзакции:
        # вторая копия задачи дождётся первой и увидит «Записан».
        upload = Upload.objects.select_for_update().select_related("seller").get(pk=upload_id)
        if upload.status != UploadStatus.WRITING:
            return
        try:
            table, parsed, unknown = parse(upload)
        except FileError as error:
            _fail(upload, str(error))
            return
        plan = plan_for(upload, parsed)
        summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows, unknown=unknown)
        how = Action(action)
        chosen = frozenset(Part(part) for part in parts) if parts is not None else ALL_PARTS
        rule = filters.parse_spec(spec)
        if how == Action.KNOWN:
            plan = plan.select(lambda item: item.site is not None)
        elif how == Action.NEW:
            rows = dict(filters.rows_from_table(table))
            plan = plan.select(
                lambda item: (
                    item.site is None
                    and filters.matches(rows.get(item.record.domain, filters.EMPTY_ROW), rule)
                )
            )
        written = Writer(upload, plan, chosen).run()
        run = {
            "action": how.value,
            "parts": sorted(part.value for part in chosen),
            "filters": rule if how == Action.NEW else {},
            "filter_text": filters.describe(rule) if how == Action.NEW else "",
            "at": timezone.now().isoformat(),
            "sites": len({item.record.domain for item in plan.items}),
            **written,
        }
        runs = [*(upload.result or {}).get("runs", []), run]
        if how == Action.ALL:
            upload.result = {**summary, **written, "runs": runs}
        else:
            # Сводка — по состоянию после записи: добавленные стали «уже в базе».
            fresh = plan_for(upload, parsed)
            upload.summary = fresh.summary(
                rows=len(table.rows), blank_rows=table.blank_rows, unknown=unknown
            )
            upload.result = {"runs": runs}
        upload.status = UploadStatus.DONE
        upload.error = None
        upload.written_at = timezone.now()
        upload.save()
    logger.info(
        "загрузка записана",
        extra={"upload_id": upload.pk, "action": action, "counts": written["counts"]},
    )


def issues(upload: Upload) -> dict[str, Any]:
    """Отклоняли, дубли, ошибки, адреса — из сводки; что перезаписано в карточке — из записей."""
    data = dict(upload.summary or {})
    changed: list[str] = []
    total = 0
    for run in (upload.result or {}).get("runs", []):
        changed.extend(run.get("facts_changed") or [])
        total += int(run.get("facts_changed_total") or 0)
    data["facts_changed"] = changed[: filters.LIST_LIMIT]
    data["facts_changed_total"] = total
    return data


def table_for(upload: Upload) -> Table:
    return _table(upload, header_row=upload.header_row)


def start(upload: Upload, status: UploadStatus) -> None:
    """Перевод в «Проверяется» или «Записывается» — задачу ставит вызывающий код."""
    upload.status = status
    upload.error = None
    upload.save(update_fields=["status", "error"])


def fail(upload_id: int, message: str) -> None:
    upload = Upload.objects.get(pk=upload_id)
    _fail(upload, message)


def _fail(upload: Upload, message: str) -> None:
    upload.status = UploadStatus.FAILED
    upload.error = message
    upload.save()


def _table(upload: Upload, *, header_row: int | None) -> Table:
    is_header = (
        catalog.looks_like_catalog
        if upload.kind == UploadKind.COLLABORATOR_CATALOG
        else looks_like_header
    )
    return read_table(file_path(upload), header_row=header_row, is_header=is_header)


def _mapping(upload: Upload) -> dict[str, Field]:
    return {key: Field(value) for key, value in (upload.mapping or {}).items()}


def _column_json(
    column: Column,
    field: Field | None = None,
    confidence: Confidence | None = None,
    hint: str = "",
) -> dict[str, Any]:
    return {
        "index": column.index,
        "letter": column.letter,
        "header": column.header,
        "key": column.key,
        "samples": list(column.samples),
        "filled": column.filled,
        "field": field.value if field else None,
        "confidence": confidence.value if confidence else None,
        "hint": hint,
    }


def _column(data: Mapping[str, Any]) -> Column:
    return Column(
        index=int(data["index"]),
        letter=str(data["letter"]),
        header=str(data["header"]),
        key=str(data["key"]),
        samples=tuple(data.get("samples") or ()),
        filled=int(data.get("filled") or 0),
    )


def _short(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    return {k: summary.get(k) for k in ("sites", "known", "new", "groups", "needs_decision")}
