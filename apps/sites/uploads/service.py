"""Путь загрузки целиком: файл → разметка → проверка → запись (ADR-044).

Экран загрузки и задачи очереди зовут только эти функции. Проверка и запись
идут в фоне: каталог Collaborator — 45 000 строк. Запись строит план заново
внутри своей транзакции — по свежему состоянию базы, а не по сводке, которую
человек видел минуту назад.

Файл размещений (ADR-051) идёт тем же путём, но со своими полями разметки и
своей памятью — по прошлым загрузкам размещений, а не у продавца.
"""

import datetime as dt
import hashlib
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.utils import timezone

from apps.content.domain_settings import offer_recheck, upload_price_cap
from apps.sites.models import Product, Seller, StatusSource, Upload, UploadKind, UploadStatus
from apps.sites.rates import latest_rates
from apps.sites.uploads import (
    ahrefs,
    anchors,
    catalog,
    filters,
    placement_columns,
    placements,
    refdomains,
)
from apps.sites.uploads.apply import ALL_PARTS, Part, Writer
from apps.sites.uploads.columns import (
    FIELD_LABELS,
    Confidence,
    Field,
    build_mapping,
    detect_currency,
    looks_like_header,
    validate_mapping,
)
from apps.sites.uploads.files import Column, FileError, Table, read_table
from apps.sites.uploads.placement_columns import PField
from apps.sites.uploads.placement_records import ParsedPlacements, parse_placements
from apps.sites.uploads.plan import Plan, build_plan, start_of_day
from apps.sites.uploads.records import Parsed, parse_price_list
from config.changes import bind_change

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
    seller: Seller | None,
    prices_date: dt.date,
    country: str | None = None,
    product: Product | None = None,
    employee: User | None = None,
    author: Any = None,
) -> Created:
    """Сохраняет файл и читает его колонки. Тот же файл с той же датой — прежняя загрузка.

    У выгрузки Ahrefs продавца нет, `country` — страна выгрузки, пусто — все.
    У размещений и ссылающихся доменов — `product`; у размещений продавец и
    сотрудник — «от кого файл», оба необязательны.
    """
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
        kind=kind,
        seller=seller,
        product=product,
        country=country or None,
        prices_date=prices_date,
        file_sha256=sha,
        status=UploadStatus.DONE,
    ).first()
    if done is not None:
        target.unlink()
        return Created(done, duplicate=True)
    upload = Upload.objects.create(
        kind=kind,
        seller=seller,
        product=product,
        employee=employee,
        country=country or None,
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
    if upload.kind == UploadKind.AHREFS_BATCH:
        upload.columns = [_column_json(c) for c in table.columns]
        upload.mapping = None
        upload.currency = None
        missing = ahrefs.missing_columns(table)
        if missing:
            _fail(
                upload,
                "Не похоже на выгрузку Ahrefs Batch Analysis: нет колонок "
                + ", ".join(f"«{name}»" for name in missing)
                + ". Нужен файл кнопки Export на странице Batch Analysis, как он скачался.",
            )
            return
        mismatch = ahrefs.country_error(table, upload.country)
        if mismatch:
            _fail(upload, mismatch)
            return
    elif upload.kind == UploadKind.REF_DOMAINS:
        upload.columns = [_column_json(c) for c in table.columns]
        upload.mapping = None
        upload.currency = None
        missing = refdomains.missing_columns(table)
        if missing:
            _fail(
                upload,
                "Не похоже на выгрузку Ahrefs «Referring domains»: нет колонок "
                + ", ".join(f"«{name}»" for name in missing)
                + ". Нужен файл кнопки Export на странице Referring domains, как он скачался.",
            )
            return
    elif upload.kind == UploadKind.COLLABORATOR_CATALOG:
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
    elif upload.kind == UploadKind.ANCHORS:
        # Память — по прошлым загрузкам анкоров (E3-05), как у размещений.
        marks, questions = anchors.build_mapping(table.columns, anchors.remembered_mapping())
        upload.columns = [
            _column_json(c, marks[c.key].field, marks[c.key].confidence, marks[c.key].hint)
            | {"ask": c.key in questions}
            for c in table.columns
        ]
        upload.mapping = {key: guess.field.value for key, guess in marks.items()}
        upload.currency = None
    elif upload.kind == UploadKind.PLACEMENTS:
        # Память — по прошлым загрузкам размещений, не у продавца (ADR-051).
        found, questions = placement_columns.build_mapping(
            table.columns, placement_columns.remembered_mapping()
        )
        fields = {key: guess.field for key, guess in found.items()}
        upload.columns = [
            _column_json(c, found[c.key].field, found[c.key].confidence, found[c.key].hint)
            | {"ask": c.key in questions}
            for c in table.columns
        ]
        upload.mapping = {key: field.value for key, field in fields.items()}
        upload.currency, _ = placement_columns.detect_currency(table.columns, fields)
    else:
        seller = upload.get_seller()
        guesses, questions = build_mapping(table.columns, seller.column_map)
        mapping = {key: guess.field for key, guess in guesses.items()}
        upload.columns = [
            _column_json(c, guesses[c.key].field, guesses[c.key].confidence, guesses[c.key].hint)
            | {"ask": c.key in questions}
            for c in table.columns
        ]
        upload.mapping = {key: field.value for key, field in mapping.items()}
        upload.currency, _ = detect_currency(table.columns, mapping, seller.currency)
    upload.summary = None
    upload.save()


def has_columns_step(upload: Upload) -> bool:
    """У загрузки есть шаг разметки колонок: прайс, файл размещений и анкоров."""
    return upload.kind in (UploadKind.PRICE_LIST, UploadKind.PLACEMENTS, UploadKind.ANCHORS)


def needs_questions(upload: Upload) -> bool:
    """Есть колонки, про которые надо спросить, или разметка с ошибкой."""
    if not has_columns_step(upload):
        return False
    if any(column.get("ask") for column in upload.columns or []):
        return True
    return bool(mapping_errors(upload, upload.mapping or {}))


def field_choices(upload: Upload) -> list[tuple[str, str]]:
    """Поля, из которых выбирают на шаге разметки: у прайса и у файла размещений свои."""
    if upload.kind == UploadKind.PLACEMENTS:
        return [(f.value, label) for f, label in placement_columns.FIELD_LABELS.items()]
    if upload.kind == UploadKind.ANCHORS:
        return [(f.value, label) for f, label in anchors.FIELD_LABELS.items()]
    return [(f.value, label) for f, label in FIELD_LABELS.items()]


def mapping_errors(upload: Upload, mapping: Mapping[str, str]) -> list[str]:
    """Ошибки разметки человеческим языком; неизвестное поле — тоже ошибка."""
    columns = [_column(c) for c in upload.columns or []]
    try:
        if upload.kind == UploadKind.PLACEMENTS:
            placement_fields = {key: PField(value) for key, value in mapping.items()}
            return placement_columns.validate_mapping(placement_fields, columns)
        if upload.kind == UploadKind.ANCHORS:
            anchor_fields = {key: anchors.AField(value) for key, value in _skip(mapping).items()}
            return anchors.validate_mapping(anchor_fields, columns)
        fields = {key: Field(value) for key, value in mapping.items()}
    except ValueError:
        return ["Неизвестное поле в разметке — обновите страницу."]
    return validate_mapping(fields, columns)


def confirm_mapping(upload: Upload, mapping: Mapping[str, str], currency: str) -> list[str]:
    """Разметка от человека: проверка и память. Ошибки — человеческим языком.

    У прайса разметка запоминается у продавца, у файла размещений — в самой
    загрузке: память по ним собирается из прошлых загрузок (ADR-051).
    """
    errors = mapping_errors(upload, mapping)
    if upload.kind == UploadKind.ANCHORS:
        # Денег в файле анкоров нет — валюта не нужна.
        if errors:
            return errors
        upload.mapping = _skip(mapping)
        upload.columns = [
            column
            | {"field": upload.mapping.get(column["key"], anchors.AField.SKIP.value), "ask": False}
            for column in upload.columns or []
        ]
        upload.save(update_fields=["mapping", "columns"])
        return []
    currency = currency.strip().upper()
    if currency not in latest_rates():
        errors.append(
            f"Для валюты {currency} нет курса ЕЦБ — с рабочими ценами не сравнить. "
            "Выберите другую валюту или обновите курсы."
        )
    if errors:
        return errors
    upload.mapping = dict(mapping)
    upload.currency = currency
    upload.columns = [
        column | {"field": upload.mapping.get(column["key"], Field.EXTRA.value), "ask": False}
        for column in upload.columns or []
    ]
    upload.save(update_fields=["mapping", "currency", "columns"])
    if upload.kind == UploadKind.PRICE_LIST:
        # Разметка запоминается у продавца: следующий файл с этими колонками — без вопросов.
        seller = upload.get_seller()
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
    if upload.seller_id is None:
        raise ValueError("план цен — только у загрузки с продавцом")
    return build_plan(
        parsed,
        seller_id=upload.seller_id,
        currency=upload.currency or "EUR",
        prices_date=upload.prices_date,
        rates=latest_rates(),
        recheck_pct=offer_recheck().min_change_pct,
    )


def placements_plan(upload: Upload) -> tuple[Table, placements.PlacementPlan]:
    """Файл размещений → план по свежему состоянию базы: сводка и запись строят его одинаково."""
    table = _table(upload, header_row=upload.header_row)
    mapping = {key: PField(value) for key, value in (upload.mapping or {}).items()}
    parsed: ParsedPlacements = parse_placements(
        table, mapping, product_domain=upload.get_product().domain
    )
    plan = placements.build_plan(
        parsed, upload=upload, rates=latest_rates(), cap=upload_price_cap()
    )
    return table, plan


def check(upload_id: int) -> None:
    """Сводка до записи: план без записи. Тело задачи очереди `upload_check`."""
    upload = Upload.objects.select_related("seller", "product", "employee").get(pk=upload_id)
    if upload.status != UploadStatus.CHECKING:
        return
    if upload.kind == UploadKind.AHREFS_BATCH:
        _check_ahrefs(upload)
        return
    if upload.kind == UploadKind.PLACEMENTS:
        _check_placements(upload)
        return
    if upload.kind == UploadKind.REF_DOMAINS:
        _check_refdomains(upload)
        return
    if upload.kind == UploadKind.ANCHORS:
        _check_anchors(upload)
        return
    try:
        table, parsed, unknown = parse(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    plan = plan_for(upload, parsed)
    upload.summary = _with_refs(
        plan.summary(rows=len(table.rows), blank_rows=table.blank_rows, unknown=unknown), parsed
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
    """Что записать: всё (прайс продавца), одной из кнопок каталога (ADR-044) или
    размещения с заменой расходящихся значений (ADR-051)."""

    ALL = "all"
    KNOWN = "known"  # «Обновить в базе» — площадки, которые уже есть
    NEW = "new"  # «Добавить новые» — только новые для базы, по фильтру
    REPLACE = "replace"  # размещения: значения файла вместо расходящихся в базе

    @property
    def label(self) -> str:
        return {
            "all": "Записать в базу",
            "known": "Обновить в базе",
            "new": "Добавить новые",
            "replace": "Записать, заменив расходящиеся",
        }[self.value]


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
        # вторая копия задачи дождётся первой и увидит «Записан». Только она
        # (of=self): продавца у выгрузки Ahrefs нет, а строку внешнего
        # соединения Postgres заблокировать не даёт.
        upload = (
            Upload.objects.select_for_update(of=("self",))
            .select_related("seller")
            .get(pk=upload_id)
        )
        if upload.status != UploadStatus.WRITING:
            return
        with journal(upload):
            _write(upload, action, parts, spec)
    logger.info("загрузка записана", extra={"upload_id": upload_id, "action": action})


@contextmanager
def journal(upload: Upload) -> Iterator[None]:
    """Всё, что запись меняет в рабочих таблицах, — в журнал загрузки (ADR-060).

    Номер загрузки — переменная транзакции, её читает триггер `log_upload_change`.
    На выходе номер снимается: дальше в той же транзакции журнал не пишется.
    """
    upload.journaled = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('seo.upload_id', %s, true)", [str(upload.pk)])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('seo.upload_id', '', true)")


def _write(
    upload: Upload,
    action: str,
    parts: Iterable[str] | None,
    spec: Mapping[str, Any] | None,
) -> None:
    """Запись загрузки по её виду — внутри транзакции, блокировки и журнала `write`."""
    if upload.kind == UploadKind.AHREFS_BATCH:
        _write_ahrefs(upload)
        return
    if upload.kind == UploadKind.PLACEMENTS:
        _write_placements(upload, replace=action == Action.REPLACE)
        return
    if upload.kind == UploadKind.REF_DOMAINS:
        _write_refdomains(upload)
        return
    if upload.kind == UploadKind.ANCHORS:
        _write_anchors(upload)
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
        upload.summary = _with_refs(
            fresh.summary(rows=len(table.rows), blank_rows=table.blank_rows, unknown=unknown),
            parsed,
        )
        upload.result = {"runs": runs}
    upload.status = UploadStatus.DONE
    upload.error = None
    upload.written_at = timezone.now()
    upload.save()


def ahrefs_plan(upload: Upload) -> tuple[Table, ahrefs.Plan]:
    """Выгрузка Ahrefs → план по свежему состоянию базы: сводка и запись строят его одинаково."""
    table = _table(upload, header_row=upload.header_row)
    mismatch = ahrefs.country_error(table, upload.country)
    if mismatch:
        raise FileError(mismatch)
    parsed = ahrefs.parse(table)
    plan = ahrefs.build_plan(
        parsed, country=upload.country, checked_at=start_of_day(upload.prices_date)
    )
    return table, plan


def _check_ahrefs(upload: Upload) -> None:
    try:
        table, plan = ahrefs_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    upload.summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    written = bool((upload.result or {}).get("runs"))
    upload.status = UploadStatus.DONE if written else UploadStatus.CHECKED
    upload.error = None
    upload.save(update_fields=["summary", "status", "error"])
    logger.info(
        "выгрузка Ahrefs проверена",
        extra={"upload_id": upload.pk, "country": upload.country, "known": len(plan.known)},
    )


def _write_ahrefs(upload: Upload) -> None:
    """Запись выгрузки Ahrefs — внутри транзакции и блокировки `write`."""
    try:
        table, plan = ahrefs_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    written = ahrefs.write(plan)
    run = {"action": Action.ALL.value, "at": timezone.now().isoformat(), **written}
    upload.summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    upload.result = {**written, "runs": [*(upload.result or {}).get("runs", []), run]}
    upload.status = UploadStatus.DONE
    upload.error = None
    upload.written_at = timezone.now()
    upload.save()
    logger.info(
        "выгрузка Ahrefs записана",
        extra={"upload_id": upload.pk, "country": upload.country, "counts": written["counts"]},
    )


def _check_placements(upload: Upload) -> None:
    try:
        table, plan = placements_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    upload.summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    written = bool((upload.result or {}).get("runs"))
    upload.status = UploadStatus.DONE if written else UploadStatus.CHECKED
    upload.error = None
    upload.save(update_fields=["summary", "status", "error"])
    logger.info(
        "файл размещений проверен",
        extra={"upload_id": upload.pk, "summary": _short(upload.summary)},
    )


def _write_placements(upload: Upload, *, replace: bool) -> None:
    """Запись файла размещений — внутри транзакции и блокировки `write`.

    Статусы площадок и размещений в истории — «загрузка» от того, кто загрузил
    (ADR-049).
    """
    try:
        table, plan = placements_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    with bind_change(StatusSource.UPLOAD, upload.author_id):
        written = placements.Writer(plan, upload, replace=replace).run()
    action = Action.REPLACE if replace else Action.ALL
    run = {"action": action.value, "at": timezone.now().isoformat(), **written}
    upload.summary = summary
    upload.result = {**summary, **written, "runs": [*(upload.result or {}).get("runs", []), run]}
    upload.status = UploadStatus.DONE
    upload.error = None
    upload.written_at = timezone.now()
    upload.save()
    logger.info(
        "файл размещений записан",
        extra={"upload_id": upload.pk, "replace": replace, "counts": written["counts"]},
    )


def _with_refs(summary: dict[str, Any], parsed: Parsed) -> dict[str, Any]:
    """Сводка прайса и каталога + сколько площадок файла уже ссылаются на продукты (ADR-051)."""
    return {**summary, "ref_domains": refdomains.linking([r.domain for r in parsed.records])}


def refdomains_plan(upload: Upload) -> tuple[Table, refdomains.Plan]:
    """Выгрузка «Referring domains» → план по свежему состоянию базы."""
    table = _table(upload, header_row=upload.header_row)
    return table, refdomains.build_plan(refdomains.parse(table), upload)


def _check_refdomains(upload: Upload) -> None:
    try:
        table, plan = refdomains_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    upload.summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    written = bool((upload.result or {}).get("runs"))
    upload.status = UploadStatus.DONE if written else UploadStatus.CHECKED
    upload.error = None
    upload.save(update_fields=["summary", "status", "error"])
    logger.info(
        "ссылающиеся домены проверены",
        extra={"upload_id": upload.pk, "domains": upload.summary["domains"]},
    )


def _write_refdomains(upload: Upload) -> None:
    """Запись выгрузки «Referring domains» — внутри транзакции и блокировки `write`."""
    try:
        table, plan = refdomains_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    summary = plan.summary(rows=len(table.rows), blank_rows=table.blank_rows)
    written = refdomains.write(plan, upload)
    run = {"action": Action.ALL.value, "at": timezone.now().isoformat(), **written}
    upload.summary = summary
    upload.result = {**written, "runs": [*(upload.result or {}).get("runs", []), run]}
    upload.status = UploadStatus.DONE
    upload.error = None
    upload.written_at = timezone.now()
    upload.save()
    logger.info(
        "ссылающиеся домены записаны",
        extra={"upload_id": upload.pk, "counts": written["counts"]},
    )


def _skip(mapping: Mapping[str, str]) -> dict[str, str]:
    """У анкоров «прочих данных» нет: колонка без поля — «не загружать»."""
    return {
        key: anchors.AField.SKIP.value if value == "extra" else value
        for key, value in mapping.items()
    }


def anchors_plan(upload: Upload) -> anchors.AnchorPlan:
    """Файл анкоров → план по свежему состоянию базы: сводка и запись строят его одинаково."""
    table = _table(upload, header_row=upload.header_row)
    mapping = {key: anchors.AField(value) for key, value in (upload.mapping or {}).items()}
    parsed = anchors.Parsed()
    anchors.parse_anchors(table, mapping, parsed)
    anchors.read_shares(file_path(upload), parsed)
    return anchors.build_plan(parsed, upload.get_product())


def _check_anchors(upload: Upload) -> None:
    try:
        plan = anchors_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    upload.summary = plan.summary()
    written = bool((upload.result or {}).get("runs"))
    upload.status = UploadStatus.DONE if written else UploadStatus.CHECKED
    upload.error = None
    upload.save(update_fields=["summary", "status", "error"])
    logger.info(
        "анкоры проверены",
        extra={"upload_id": upload.pk, "anchors": upload.summary["anchors"]},
    )


def _write_anchors(upload: Upload) -> None:
    """Запись файла анкоров — внутри транзакции и блокировки `write`."""
    try:
        plan = anchors_plan(upload)
    except FileError as error:
        _fail(upload, str(error))
        return
    summary = plan.summary()
    counts = anchors.write(plan)
    run = {"action": Action.ALL.value, "at": timezone.now().isoformat(), "counts": counts}
    upload.summary = summary
    upload.result = {"counts": counts, "runs": [*(upload.result or {}).get("runs", []), run]}
    upload.status = UploadStatus.DONE
    upload.error = None
    upload.written_at = timezone.now()
    upload.save()
    logger.info("анкоры записаны", extra={"upload_id": upload.pk, "counts": counts})


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
    is_header = {
        UploadKind.COLLABORATOR_CATALOG: catalog.looks_like_catalog,
        UploadKind.AHREFS_BATCH: ahrefs.looks_like_batch,
        UploadKind.PLACEMENTS: placement_columns.looks_like_placements_header,
        UploadKind.REF_DOMAINS: refdomains.looks_like_refdomains,
        UploadKind.ANCHORS: anchors.looks_like_anchors_header,
    }.get(UploadKind(upload.kind), looks_like_header)
    # В книге анкоров нужный лист — не первый: берём тот, где узнаётся заголовок.
    return read_table(
        file_path(upload),
        header_row=header_row,
        is_header=is_header,
        pick_sheet=upload.kind == UploadKind.ANCHORS,
    )


def _mapping(upload: Upload) -> dict[str, Field]:
    return {key: Field(value) for key, value in (upload.mapping or {}).items()}


def _column_json(
    column: Column,
    field: StrEnum | None = None,
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
