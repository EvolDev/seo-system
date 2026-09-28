"""Проверка очереди: воркер жив, журнал пишется, повторы и алерт работают (E2-01).

    python manage.py queue_check                        # проба — «Успешно» в журнале
    python manage.py queue_check --fail                 # падает на всех попытках: «Ошибка», алерт
    python manage.py queue_check --sleep 60 --wait 0    # долгая проба: пока идёт, остановите воркер
    python manage.py queue_check --count 5 --throttle   # старты не чаще раза в секунду

Пригодится и после деплоя, как `observability_check`. Пробы ставятся в
очередь с одним новым `run_id` — по нему их строки находятся в журнале
и в «Запусках задач».
"""

import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from apps.observability.admin import duration_text
from apps.observability.models import TaskRun, TaskStatus
from apps.observability.tasks import queue_probe
from config.run_id import bind_run_id, new_run_id

POLL_SECONDS = 0.5


class Command(BaseCommand):
    help = "Ставит в очередь пробные задачи и показывает их запуски из журнала."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--fail",
            action="store_true",
            help="проба падает на каждой попытке: «Ошибка» в журнале и событие в Sentry",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0,
            dest="sleep_seconds",
            metavar="СЕКУНД",
            help="проба идёт столько секунд — проверить остановку воркера",
        )
        parser.add_argument("--count", type=int, default=1, help="сколько проб поставить")
        parser.add_argument(
            "--throttle",
            action="store_true",
            help="у всех проб один ключ ограничения скорости: старты не чаще раза в секунду",
        )
        parser.add_argument(
            "--wait",
            type=float,
            default=60,
            metavar="СЕКУНД",
            help="сколько ждать результата (по умолчанию 60); 0 — не ждать",
        )

    def handle(
        self,
        *args: Any,
        fail: bool,
        sleep_seconds: float,
        count: int,
        throttle: bool,
        wait: float,
        **options: Any,
    ) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            for _ in range(count):
                queue_probe.delay(
                    fail=fail,
                    sleep_seconds=sleep_seconds,
                    throttle_key=str(run_id) if throttle else None,
                )
        self.stdout.write(f"run_id: {run_id} · проб в очереди: {count}")
        if not wait:
            self.stdout.write("Не ждём. Строки проб — в «Запусках задач», поиск по run_id.")
            return

        runs = self._wait(run_id, count, wait)
        for run in runs:
            self.stdout.write(_line(run))
        finished = [run for run in runs if run.status != TaskStatus.RUNNING]
        if len(finished) < count:
            self.stdout.write(
                f"За {wait:g} с закончено проб: {len(finished)} из {count}. "
                "Воркер запущен? docker compose ps; его лог — docker compose logs worker."
            )
        if fail and any(run.status == TaskStatus.FAILED for run in runs):
            self.stdout.write(
                f"Событие в Sentry отправил воркер: ищите по тегу run_id:{run_id}. "
                "Нет события — пуст SENTRY_DSN или смотрите лог воркера."
            )

    def _wait(self, run_id: Any, count: int, wait: float) -> list[TaskRun]:
        """Строки проб из журнала, когда все закончились или вышло время."""
        deadline = time.monotonic() + wait
        while True:
            runs = list(TaskRun.objects.filter(run_id=run_id).order_by("started_at", "pk"))
            done = len(runs) >= count and all(run.status != TaskStatus.RUNNING for run in runs)
            if done or time.monotonic() >= deadline:
                return runs
            time.sleep(POLL_SECONDS)


def _line(run: TaskRun) -> str:
    """«14:02:11.123 · Успешно · попытка 1 · 0,1 с» — старт с миллисекундами."""
    started = timezone.localtime(run.started_at)
    parts = [
        f"{started:%H:%M:%S}.{started.microsecond // 1000:03d}",
        TaskStatus(run.status).label,
        f"попытка {(run.payload or {}).get('attempt', 1)}",
    ]
    duration = duration_text(run.duration_ms)
    if duration:
        parts.append(duration)
    if run.error:
        parts.append(run.error)
    return " · ".join(parts)
