"""Здоровье прода: архив базы, свежие бэкапы, проверки восстановления, диск (ADR-081)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

from apps.observability import health
from apps.observability.health import Archiver, Problem
from apps.observability.models import BackupKind, BackupRun, BackupStatus, BackupTarget
from apps.observability.tasks import system_health

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _r2_enabled(settings: Any) -> None:
    """По умолчанию — оба хранилища; без R2 — `TestWithoutR2`."""
    settings.BACKUP_R2_ENABLED = True


def _run(kind: str, target: str, hours_ago: float, status: str = BackupStatus.OK) -> BackupRun:
    run = BackupRun.objects.create(kind=kind, target=target, status=status, message="сообщение")
    started = NOW - timedelta(hours=hours_ago)
    BackupRun.objects.filter(pk=run.pk).update(started_at=started, finished_at=started)
    run.refresh_from_db()
    return run


def _all_fresh() -> None:
    for kind, target in health.expected():
        _run(kind, target, hours_ago=5)


def _keys(problems: list[Problem]) -> set[str]:
    return {problem.key for problem in problems}


class TestArchive:
    def test_working_archive_is_fine(self) -> None:
        state = Archiver("on", NOW - timedelta(minutes=3), None, None)
        assert health.archive_problems(state, NOW) == []

    def test_off(self) -> None:
        assert _keys(health.archive_problems(Archiver("off", None, None, None), NOW)) == {
            "archive-off"
        }

    def test_failing_after_last_success(self) -> None:
        state = Archiver("on", NOW - timedelta(hours=1), NOW - timedelta(minutes=1), "0000000100")
        [problem] = health.archive_problems(state, NOW)
        assert problem.key == "archive-failing"
        assert "0000000100" in problem.text

    def test_old_failure_then_success_is_fine(self) -> None:
        state = Archiver("on", NOW - timedelta(minutes=1), NOW - timedelta(hours=5), "x")
        assert health.archive_problems(state, NOW) == []

    def test_stale(self, settings: Any) -> None:
        settings.WAL_ARCHIVE_MAX_AGE_MINUTES = 120
        state = Archiver("on", NOW - timedelta(hours=3), None, None)
        assert _keys(health.archive_problems(state, NOW)) == {"archive-stale"}


class TestBackups:
    def test_all_fresh(self) -> None:
        _all_fresh()
        assert health.backup_problems(NOW) == []

    def test_nothing_yet_names_every_backup(self) -> None:
        keys = _keys(health.backup_problems(NOW))
        assert keys == {f"backup-stale-{kind}-{target}" for kind, target in health.expected()}

    def test_dump_older_than_a_day(self, settings: Any) -> None:
        settings.BACKUP_MAX_AGE_HOURS = 26
        _all_fresh()
        _run(BackupKind.DUMP, BackupTarget.R2, hours_ago=30)
        BackupRun.objects.filter(
            kind=BackupKind.DUMP, target=BackupTarget.R2, started_at__gt=NOW - timedelta(hours=6)
        ).delete()
        assert _keys(health.backup_problems(NOW)) == {"backup-stale-dump-r2"}

    def test_restore_test_has_a_week(self, settings: Any) -> None:
        settings.RESTORE_TEST_MAX_AGE_DAYS = 8
        _all_fresh()
        BackupRun.objects.filter(kind=BackupKind.RESTORE_TEST).delete()
        _run(BackupKind.RESTORE_TEST, BackupTarget.B2, hours_ago=6 * 24)
        _run(BackupKind.RESTORE_TEST, BackupTarget.R2, hours_ago=9 * 24)
        assert _keys(health.backup_problems(NOW)) == {"backup-stale-restore_test-r2"}

    def test_failed_last_attempt_reported_at_once(self) -> None:
        _all_fresh()
        _run(BackupKind.BASE, BackupTarget.B2, hours_ago=1, status=BackupStatus.FAILED)
        [problem] = health.backup_problems(NOW)
        assert problem.key == "backup-failed-base-b2"
        assert "сообщение" in problem.text

    def test_running_does_not_hide_last_result(self) -> None:
        _all_fresh()
        _run(BackupKind.BASE, BackupTarget.B2, hours_ago=0.1, status=BackupStatus.RUNNING)
        assert health.backup_problems(NOW) == []


class TestWithoutR2:
    """Второе хранилище выключено: копия и проверка только в B2, R2 не ждём."""

    @pytest.fixture(autouse=True)
    def _r2_off(self, settings: Any) -> None:
        settings.BACKUP_R2_ENABLED = False

    def test_r2_not_expected(self) -> None:
        assert {target for _, target in health.expected()} == {BackupTarget.B2}
        assert len(health.expected()) == 4

    def test_b2_fresh_is_enough(self) -> None:
        _all_fresh()
        assert not BackupRun.objects.filter(target=BackupTarget.R2).exists()
        assert health.backup_problems(NOW) == []

    def test_b2_still_watched(self, settings: Any) -> None:
        settings.BACKUP_MAX_AGE_HOURS = 26
        _run(BackupKind.BASE, BackupTarget.B2, hours_ago=5)
        _run(BackupKind.DUMP, BackupTarget.B2, hours_ago=30)
        _run(BackupKind.UPLOADS, BackupTarget.B2, hours_ago=5)
        _run(BackupKind.RESTORE_TEST, BackupTarget.B2, hours_ago=5)
        assert _keys(health.backup_problems(NOW)) == {"backup-stale-dump-b2"}

    def test_old_r2_failure_ignored(self) -> None:
        """R2 выключили после неудачи — старая строка журнала не тревожит."""
        _all_fresh()
        _run(BackupKind.DUMP, BackupTarget.R2, hours_ago=1, status=BackupStatus.FAILED)
        assert health.backup_problems(NOW) == []

    def test_summary_shows_b2_copy(self, admin_client: Client) -> None:
        _run(BackupKind.DUMP, BackupTarget.B2, hours_ago=1)
        content = admin_client.get(
            reverse("admin:observability_backuprun_changelist")
        ).content.decode()
        assert "дамп B2:" in content and "файлы B2: не было" in content
        assert "R2" not in health.summary()


class TestDisk:
    def test_full_disk(self, settings: Any) -> None:
        settings.DISK_ALERT_PERCENT = 80
        usage = mock.Mock(total=100 * 1024**3, used=85 * 1024**3, free=15 * 1024**3)
        with mock.patch("apps.observability.health.shutil.disk_usage", return_value=usage):
            [problem] = health.disk_problems()
        assert problem.key == "disk-full"
        assert "85 %" in problem.text

    def test_enough_space(self, settings: Any) -> None:
        settings.DISK_ALERT_PERCENT = 80
        usage = mock.Mock(total=100, used=50, free=50)
        with mock.patch("apps.observability.health.shutil.disk_usage", return_value=usage):
            assert health.disk_problems() == []


class TestTask:
    def test_local_version_checks_nothing(self, settings: Any) -> None:
        settings.BACKUP_MONITOR = False
        assert health.check(NOW) == []

    def test_reads_real_archiver(self) -> None:
        """В тестовой базе архив выключен — так и читается из pg_stat_archiver."""
        assert health.archiver().mode == "off"

    def test_problem_becomes_event_with_daily_fingerprint(
        self, settings: Any, sentry_events: Any
    ) -> None:
        settings.BACKUP_MONITOR = True
        usage = mock.Mock(total=100, used=10, free=90)
        with mock.patch("apps.observability.health.shutil.disk_usage", return_value=usage):
            assert system_health.delay().get() == 1 + len(health.expected())
        events = [event for event in sentry_events if event.get("level") == "error"]
        fingerprints = {tuple(event["fingerprint"][:2]) for event in events}
        assert ("system-health", "archive-off") in fingerprints
        assert ("system-health", "backup-stale-base-b2") in fingerprints


class TestScreen:
    def test_list_with_summary(self, admin_client: Client) -> None:
        _run(BackupKind.BASE, BackupTarget.B2, hours_ago=2)
        _run(BackupKind.DUMP, BackupTarget.R2, hours_ago=1, status=BackupStatus.FAILED)
        BackupRun.objects.filter(kind=BackupKind.BASE).update(size_bytes=39_000_000)
        response = admin_client.get(reverse("admin:observability_backuprun_changelist"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "✓ Готово" in content and "✗ Ошибка" in content
        assert "37,2 МБ" in content
        assert "Архив изменений базы: выключен" in content
        assert "дамп R2: не было" in content

    def test_read_only(self, admin_client: Client) -> None:
        run = _run(BackupKind.BASE, BackupTarget.B2, hours_ago=1)
        assert admin_client.get(reverse("admin:observability_backuprun_add")).status_code == 403
        response = admin_client.get(reverse("admin:observability_backuprun_change", args=[run.pk]))
        assert response.status_code == 200
        assert 'name="_save"' not in response.content.decode()
