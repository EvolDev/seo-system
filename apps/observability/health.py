"""Здоровье прода раз в час: архив базы, свежие бэкапы, проверки восстановления, диск.

E10-03, E10-05, ADR-081. Бэкап, о пропаже которого никто не узнал, не
существует: задача `system_health` раз в час смотрит

- архив изменений базы (WAL) — `pg_stat_archiver`: файлы уходят в B2, свежий
  ушёл недавно, последняя попытка не упала;
- журнал `backup_runs`, который пишет контейнер `backup`: полный снимок базы
  (B2), ночной дамп и файлы загрузок (B2 и R2) — не старше
  `BACKUP_MAX_AGE_HOURS`, проверки восстановления (B2 и R2) — не старше
  `RESTORE_TEST_MAX_AGE_DAYS`; последняя попытка не упала. R2 — только когда
  второе хранилище включено (`BACKUP_R2_ENABLED`);
- место на диске сервера — не больше `DISK_ALERT_PERCENT` %.

Что не так — событие в GlitchTip (оттуда письмо), одно в день на каждую беду.
В локальной версии бэкапов нет: проверка включается `BACKUP_MONITOR`.
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sentry_sdk
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.observability.models import BackupKind, BackupRun, BackupStatus, BackupTarget

logger = logging.getLogger(__name__)

# Что и куда должно уходить: полный снимок — только в B2 (к нему архив WAL),
# дамп и файлы загрузок — в каждое хранилище, проверка — каждого хранилища.
EXPECTED: tuple[tuple[BackupKind, BackupTarget], ...] = (
    (BackupKind.BASE, BackupTarget.B2),
    (BackupKind.DUMP, BackupTarget.B2),
    (BackupKind.DUMP, BackupTarget.R2),
    (BackupKind.UPLOADS, BackupTarget.B2),
    (BackupKind.UPLOADS, BackupTarget.R2),
    (BackupKind.RESTORE_TEST, BackupTarget.B2),
    (BackupKind.RESTORE_TEST, BackupTarget.R2),
)


def expected() -> tuple[tuple[BackupKind, BackupTarget], ...]:
    """Что и куда должно уходить сейчас: R2 — только когда он включён."""
    if settings.BACKUP_R2_ENABLED:
        return EXPECTED
    return tuple(pair for pair in EXPECTED if pair[1] != BackupTarget.R2)


@dataclass(frozen=True)
class Problem:
    """Беда: `key` — отпечаток события (одно в день на беду), `text` — для человека."""

    key: str
    text: str


@dataclass(frozen=True)
class Archiver:
    """Состояние архива изменений базы — строка `pg_stat_archiver`."""

    mode: str
    last_archived_at: datetime | None
    last_failed_at: datetime | None
    last_failed_wal: str | None


def archiver() -> Archiver:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('archive_mode'), last_archived_time,"
            " last_failed_time, last_failed_wal FROM pg_stat_archiver"
        )
        mode, archived, failed, failed_wal = cursor.fetchone()
    return Archiver(str(mode), archived, failed, failed_wal)


def archive_problems(state: Archiver, now: datetime) -> list[Problem]:
    if state.mode == "off":
        return [Problem("archive-off", "Архив изменений базы выключен (archive_mode=off).")]
    if state.last_failed_at and (
        state.last_archived_at is None or state.last_failed_at > state.last_archived_at
    ):
        return [
            Problem(
                "archive-failing",
                f"Изменения базы не уходят в архив B2: файл {state.last_failed_wal} не"
                f" отправился в {_when(state.last_failed_at)}. Журнал копится на диске сервера.",
            )
        ]
    limit = timedelta(minutes=settings.WAL_ARCHIVE_MAX_AGE_MINUTES)
    if state.last_archived_at is None or now - state.last_archived_at > limit:
        return [
            Problem(
                "archive-stale",
                "Изменения базы давно не уходят в архив: последний файл —"
                f" {_when(state.last_archived_at)}.",
            )
        ]
    return []


def latest_runs() -> dict[tuple[str, str], dict[str, BackupRun | None]]:
    """Для каждого «что и куда» — последняя удачная и самая последняя запись."""
    result: dict[tuple[str, str], dict[str, BackupRun | None]] = {}
    for kind, target in expected():
        runs = BackupRun.objects.filter(kind=kind, target=target).order_by("-started_at", "-pk")
        result[(kind, target)] = {
            "ok": runs.filter(status=BackupStatus.OK).first(),
            "last": runs.exclude(status=BackupStatus.RUNNING).first(),
        }
    return result


def backup_problems(now: datetime) -> list[Problem]:
    problems = []
    for (kind, target), runs in latest_runs().items():
        label = f"{BackupKind(kind).label} → {BackupTarget(target).label}"
        last, ok = runs["last"], runs["ok"]
        if last is not None and last.status == BackupStatus.FAILED:
            problems.append(
                Problem(
                    f"backup-failed-{kind}-{target}",
                    f"{label}: последняя попытка {_when(last.started_at)} не удалась —"
                    f" {last.message or 'без сообщения'}.",
                )
            )
            continue
        if kind == BackupKind.RESTORE_TEST:
            limit = timedelta(days=settings.RESTORE_TEST_MAX_AGE_DAYS)
        else:
            limit = timedelta(hours=settings.BACKUP_MAX_AGE_HOURS)
        if ok is None or now - ok.started_at > limit:
            when = _when(ok.started_at) if ok else "ни разу"
            problems.append(
                Problem(f"backup-stale-{kind}-{target}", f"{label}: последний удачный — {when}.")
            )
    return problems


def disk_problems() -> list[Problem]:
    usage = shutil.disk_usage(settings.UPLOADS_DIR)
    percent = usage.used * 100 / usage.total
    if percent <= settings.DISK_ALERT_PERCENT:
        return []
    free_gb = usage.free / 1024**3
    return [
        Problem(
            "disk-full",
            f"Диск сервера занят на {percent:.0f} %, свободно {free_gb:.1f} ГБ.",
        )
    ]


def check(now: datetime | None = None) -> list[Problem]:
    """Все беды прода сейчас; в локальной версии — ничего (`BACKUP_MONITOR`)."""
    if not settings.BACKUP_MONITOR:
        return []
    now = now or timezone.now()
    return [*archive_problems(archiver(), now), *backup_problems(now), *disk_problems()]


def report(problems: list[Problem], now: datetime | None = None) -> None:
    """Каждая беда — своё событие GlitchTip; повтор в тот же день — в то же событие."""
    day = timezone.localdate(now or timezone.now()).isoformat()
    for problem in problems:
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["system-health", problem.key, day]
            logger.error(problem.text, extra={"problem": problem.key})


def summary() -> str:
    """Одна строка над журналом «Бэкапы»: когда что последний раз удалось."""
    parts: list[str] = []
    try:
        state = archiver()
    except Exception:
        state = None
    if state is not None:
        if state.mode == "off":
            parts.append("Архив изменений базы: выключен")
        else:
            parts.append(f"Архив изменений базы: {_when(state.last_archived_at)}")
    # Ночная копия — по хранилищу у другой компании, если оно есть, иначе по B2.
    copy_target = BackupTarget.R2 if settings.BACKUP_R2_ENABLED else BackupTarget.B2
    for (kind, target), runs in latest_runs().items():
        ok = runs["ok"]
        if kind in (BackupKind.BASE, BackupKind.RESTORE_TEST) or target == copy_target:
            short = {
                BackupKind.BASE: "полный снимок",
                BackupKind.DUMP: "дамп",
                BackupKind.UPLOADS: "файлы",
                BackupKind.RESTORE_TEST: "проверка",
            }[BackupKind(kind)]
            parts.append(f"{short} {target.upper()}: {_when(ok.started_at) if ok else 'не было'}")
    return " · ".join(parts)


def _when(moment: Any) -> str:
    if moment is None:
        return "не было"
    return timezone.localtime(moment).strftime("%d.%m %H:%M")
