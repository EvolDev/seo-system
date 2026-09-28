"""Импорт таблицы ручной работы (E1-04, `docs/12-IMPORT-MAPPING.md`).

    python manage.py import_workbook "Линкбилдинг Convertio.co.xlsx" \\
        --date 2026-09-27 --list "Сентябрь 2026" --dry-run

Без `--dry-run` — запись. Повторный запуск того же файла ничего не
дублирует. Полный отчёт — файлом в `import-reports/`.
"""

import argparse
import datetime as dt
import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.sites.importing.run import ImportOptions, run_import
from apps.sites.importing.workbook import ImportAbort
from config.run_id import bind_run_id, new_run_id

logger = logging.getLogger(__name__)

REPORT_DIR = "import-reports"


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"дата в виде ГГГГ-ММ-ДД, а не {value!r}") from None


class Command(BaseCommand):
    help = "Импорт таблицы: площадки, замеры, решения, размещения, ключи и позиции (E1-04)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", type=Path, help="файл xlsx")
        parser.add_argument(
            "--date",
            required=True,
            type=_date,
            help="дата выгрузки, ГГГГ-ММ-ДД: на неё пишутся метрики и цены",
        )
        parser.add_argument(
            "--list",
            required=True,
            dest="list_name",
            help="рабочий список площадок, например «Сентябрь 2026»; есть — дополняется",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="всё посчитать и показать, ничего не записать"
        )
        parser.add_argument(
            "--report-dir",
            type=Path,
            default=None,
            help=f"куда положить полный отчёт; по умолчанию {REPORT_DIR}/ в корне проекта",
        )

    def handle(
        self,
        *args: Any,
        path: Path,
        date: dt.date,
        list_name: str,
        dry_run: bool,
        report_dir: Path | None,
        **options: Any,
    ) -> None:
        options_ = ImportOptions(path=path, as_of=date, list_name=list_name, dry_run=dry_run)
        # Импорт — точка входа цепочки: всё, что он пишет и логирует,
        # помечено одним run_id (ADR-025).
        with bind_run_id(new_run_id()):
            logger.info("импорт начат", extra={"file": path.name, "dry_run": dry_run})
            try:
                result = run_import(options_, report_dir or Path(settings.BASE_DIR) / REPORT_DIR)
            except ImportAbort as error:
                raise CommandError(str(error)) from error

        self.stdout.write(result.report.summary())
        self.stdout.write(f"Полный отчёт: {result.report_path}")
        if dry_run:
            self.stdout.write("Сухой прогон: в базе ничего не изменилось.")
