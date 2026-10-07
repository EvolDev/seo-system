"""Копия «площадки на сегодня»: пересчёт и сверка (E1-11, ADR-065).

    python manage.py site_latest --rebuild   # пересчитать всё
    python manage.py site_latest --check     # сверить с живым подсчётом

Пересчёт нужен после восстановления базы из дампа и если сверка нашла
расхождение. В обычной работе копия пересчитывается сама: при загрузке,
импорте, смене рабочей цены и смене курсов валют.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.sites import latest
from config.run_id import bind_run_id, new_run_id


class Command(BaseCommand):
    help = "Пересчитать или сверить копию «площадки на сегодня» (E1-11)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--rebuild", action="store_true", help="пересчитать все площадки")
        parser.add_argument("--check", action="store_true", help="сверить копию с живым подсчётом")
        parser.add_argument(
            "--limit", type=int, default=50, help="сколько расхождений показать (по умолчанию 50)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["rebuild"] and not options["check"]:
            raise CommandError("нужен --rebuild или --check")
        if options["rebuild"]:
            with bind_run_id(new_run_id()):
                rows = latest.refresh_all()
            self.stdout.write(f"Пересчитано площадок: {rows}")
        if options["check"]:
            found = latest.stale(limit=options["limit"])
            if not found:
                self.stdout.write("Копия совпадает с живым подсчётом.")
                return
            self.stdout.write(f"Расхождений: {len(found)} (показаны первые {options['limit']})")
            for row in found:
                self.stdout.write(f"  {row['site_id']} · {row['domain']}")
            raise CommandError("копия разошлась — нужен --rebuild")
