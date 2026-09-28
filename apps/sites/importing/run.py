"""Один запуск импорта: чтение файла, транзакция, отчёт, журнал запусков.

Порядок (маппинг §3): файл читается и разбирается целиком до первой
записи; затем в одной транзакции — ключи, площадки, сверки. Сухой
прогон — тот же путь, но транзакция в конце откатывается, поэтому отчёт
сухого прогона совпадает с настоящим.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.observability.models import TaskRun, TaskStatus
from apps.sites.importing.apply import Importer, compare_copy
from apps.sites.importing.report import Report
from apps.sites.importing.rows import (
    BASE_REQUIRED,
    BASE_SHEET,
    COPY_REQUIRED,
    COPY_SHEET,
    KEYWORDS_REQUIRED,
    KEYWORDS_SHEET,
    LINKS_PLACED,
    LINKS_WAITING,
    parse_base,
    parse_copy,
    parse_keywords,
)
from apps.sites.importing.workbook import open_workbook, read_sheet
from config.run_id import current_run_id

logger = logging.getLogger(__name__)

TASK_NAME = "import_workbook"


@dataclass(frozen=True)
class ImportOptions:
    path: Path
    as_of: dt.date  # дата выгрузки: на неё пишутся снапшоты
    list_name: str
    dry_run: bool


@dataclass(frozen=True)
class ImportResult:
    report: Report
    report_path: Path
    run_id: UUID | None


def run_import(options: ImportOptions, report_dir: Path) -> ImportResult:
    """Импорт целиком. Ошибка — исключение, в базе ничего не остаётся.

    Запуск пишется в `task_runs` вне транзакции импорта: и сухой прогон,
    и упавший запуск остаются в журнале.
    """
    started = time.monotonic()
    task = TaskRun.objects.create(
        task_name=TASK_NAME,
        status=TaskStatus.RUNNING,
        payload={
            "file": options.path.name,
            "date": options.as_of.isoformat(),
            "list": options.list_name,
            "dry_run": options.dry_run,
        },
    )
    report = Report()
    try:
        _import(options, report)
    except Exception as error:
        _finish(task, started, TaskStatus.FAILED, error=str(error))
        raise
    report_path = _write_report(options, report, report_dir)
    payload = {**(task.payload or {}), **report.as_payload(), "report": report_path.name}
    _finish(task, started, TaskStatus.SUCCESS, payload=payload)
    logger.info(
        "импорт завершён",
        extra={"dry_run": options.dry_run, "counts": payload["counts"], "report": str(report_path)},
    )
    return ImportResult(report, report_path, current_run_id())


def _import(options: ImportOptions, report: Report) -> None:
    with open_workbook(options.path) as workbook:
        base_sheet = read_sheet(workbook, BASE_SHEET, BASE_REQUIRED)
        keywords_sheet = read_sheet(workbook, KEYWORDS_SHEET, KEYWORDS_REQUIRED)
        copy_sheet = None
        if COPY_SHEET in workbook.sheetnames:
            copy_sheet = read_sheet(workbook, COPY_SHEET, COPY_REQUIRED)
    sites = parse_base(base_sheet, report)
    keywords = parse_keywords(keywords_sheet, report)
    copy = parse_copy(copy_sheet, report) if copy_sheet else None
    if copy is None:
        report.note(f"Вкладки «{COPY_SHEET}» нет — сверка с ней не делалась")

    # transaction.atomic — всё внутри блока применяется целиком или никак:
    # исключение откатывает все записи, как BEGIN … ROLLBACK в SQL.
    with transaction.atomic():
        importer = Importer(
            as_of=options.as_of,
            list_name=options.list_name,
            source=options.path.name,
            report=report,
        )
        saved_keywords = importer.import_keywords(keywords)
        importer.import_sites(sites, saved_keywords)
        if {LINKS_PLACED, LINKS_WAITING} <= set(keywords_sheet.headers):
            importer.compare_link_counts(keywords, saved_keywords)
        if copy is not None:
            compare_copy(copy, sites, report)
        if options.dry_run:
            transaction.set_rollback(True)


def _write_report(options: ImportOptions, report: Report, report_dir: Path) -> Path:
    now = timezone.localtime()
    mode = "dry-run" if options.dry_run else "import"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{now:%Y-%m-%d_%H%M%S}_{mode}.md"
    header = {
        "Файл": options.path.name,
        "Дата выгрузки": f"{options.as_of:%d.%m.%Y}",
        "Список": options.list_name,
        "Режим": "сухой прогон, в базе ничего не изменилось" if options.dry_run else "запись",
        "Запуск": f"{now:%d.%m.%Y %H:%M:%S}",
        "run_id": str(current_run_id() or "—"),
    }
    path.write_text(report.to_markdown("Отчёт импорта таблицы", header), encoding="utf-8")
    return path


def _finish(
    task: TaskRun,
    started: float,
    status: TaskStatus,
    *,
    error: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    task.status = status
    task.finished_at = timezone.now()
    task.duration_ms = int((time.monotonic() - started) * 1000)
    task.error = error
    if payload is not None:
        task.payload = payload
    task.save(update_fields=["status", "finished_at", "duration_ms", "error", "payload"])
