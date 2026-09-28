"""Блок 6: журнал наблюдаемости, `run_id`, `v_overdue_checks` (E1-03)."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.db.models import ProtectedError
from django.utils import timezone

from apps.content.models import PromptTemplate, PromptVariant
from apps.observability.models import (
    ApiUsage,
    Check,
    CheckStatus,
    LlmCall,
    LlmStatus,
    Performer,
    TaskRun,
    TaskStatus,
)
from config.run_id import bind_run_id

pytestmark = pytest.mark.django_db

TABLES = ["checks", "llm_calls", "task_runs", "api_usage"]


def _create_all() -> list[Any]:
    """По одной строке в каждой из четырёх таблиц."""
    return [
        Check.objects.create(
            entity_type="site", entity_id=1, check_type="indexation", status=CheckStatus.OK
        ),
        LlmCall.objects.create(task="audit", model="some-model", status=LlmStatus.OK),
        TaskRun.objects.create(task_name="check_indexation", status=TaskStatus.RUNNING),
        ApiUsage.objects.create(provider="ahrefs", units=1),
    ]


def _check(entity_id: int, checked_at: datetime, next_check_at: datetime | None) -> Check:
    # checked_at задаём явно: у строк одной транзакции now() одинаковое,
    # и «последняя проверка» была бы не определена.
    return Check.objects.create(
        entity_type="placement_link",
        entity_id=entity_id,
        check_type="link_alive",
        status=CheckStatus.OK,
        checked_at=checked_at,
        next_check_at=next_check_at,
    )


def _overdue() -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT entity_type, entity_id, check_type, last_checked, due_at"
            " FROM v_overdue_checks ORDER BY entity_id"
        )
        return list(cursor.fetchall())


class TestRunId:
    def test_column_in_all_four_tables(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = 'run_id'
                  AND data_type = 'uuid' AND table_name = ANY(%s)
                """,
                [TABLES],
            )
            assert sorted(row[0] for row in cursor.fetchall()) == sorted(TABLES)

    def test_taken_from_current_chain(self) -> None:
        with bind_run_id(uuid4()) as run_id:
            rows = _create_all()
        for row in rows:
            row.refresh_from_db()
            assert row.run_id == run_id, type(row).__name__

    def test_empty_outside_chain(self) -> None:
        for row in _create_all():
            row.refresh_from_db()
            assert row.run_id is None, type(row).__name__

    def test_explicit_value_wins(self) -> None:
        other = UUID("00000000-0000-0000-0000-000000000001")
        with bind_run_id(uuid4()):
            check = Check.objects.create(
                entity_type="site",
                entity_id=1,
                check_type="indexation",
                status=CheckStatus.OK,
                run_id=other,
            )
        check.refresh_from_db()
        assert check.run_id == other


class TestDefaults:
    def test_database_defaults(self) -> None:
        check, llm_call, task_run, usage = _create_all()
        for row in (check, llm_call, task_run, usage):
            row.refresh_from_db()
        assert check.performed_by == Performer.SYSTEM
        assert check.checked_at is not None
        assert check.next_check_at is None
        assert llm_call.currency == "USD"
        assert llm_call.created_at is not None
        assert task_run.started_at is not None
        assert usage.currency == "USD"

    def test_insert_bypassing_django_gets_same_defaults(self) -> None:
        # Значения по умолчанию живут в базе (ADR-029), не только в модели.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO checks (entity_type, entity_id, check_type, status)"
                " VALUES ('site', 1, 'indexation', 'ok')"
                " RETURNING performed_by, checked_at"
            )
            performed_by, checked_at = cursor.fetchone()
        assert performed_by == Performer.SYSTEM
        assert checked_at is not None


class TestLlmCallPromptVariant:
    def test_stores_link_to_variant(self) -> None:
        template = PromptTemplate.objects.create(task="audit", name="Аудит площадки")
        variant = PromptVariant.objects.create(template=template, label="v1", body="…")
        call = LlmCall.objects.create(
            task="audit", model="some-model", status=LlmStatus.OK, prompt_variant=variant
        )
        call.refresh_from_db()
        assert call.prompt_variant == variant

    def test_variant_with_calls_cannot_be_deleted(self) -> None:
        template = PromptTemplate.objects.create(task="audit", name="Аудит площадки")
        variant = PromptVariant.objects.create(template=template, label="v1", body="…")
        LlmCall.objects.create(
            task="audit", model="some-model", status=LlmStatus.OK, prompt_variant=variant
        )
        with pytest.raises(ProtectedError):
            variant.delete()

    def test_variant_counters_start_at_zero(self) -> None:
        template = PromptTemplate.objects.create(task="audit", name="Аудит площадки")
        variant = PromptVariant.objects.create(template=template, label="v1", body="…")
        variant.refresh_from_db()
        assert (variant.times_used, variant.times_accepted, variant.is_active) == (0, 0, True)
        assert template.product is None


class TestOverdueChecks:
    def test_empty_on_fresh_database(self) -> None:
        # Критерий приёмки E1-05.
        assert _overdue() == []

    def test_past_due_is_overdue(self) -> None:
        # Критерий приёмки E1-03.
        now = timezone.now()
        check = _check(1, checked_at=now - timedelta(days=8), next_check_at=now - timedelta(days=1))
        assert _overdue() == [
            ("placement_link", 1, "link_alive", check.checked_at, check.next_check_at)
        ]

    def test_future_due_is_not_overdue(self) -> None:
        now = timezone.now()
        _check(1, checked_at=now, next_check_at=now + timedelta(days=7))
        assert _overdue() == []

    def test_no_next_check_is_not_overdue(self) -> None:
        _check(1, checked_at=timezone.now(), next_check_at=None)
        assert _overdue() == []

    def test_newer_check_clears_old_overdue(self) -> None:
        # Срок берётся из последней проверки, а не из всей истории.
        now = timezone.now()
        _check(1, checked_at=now - timedelta(days=8), next_check_at=now - timedelta(days=1))
        _check(1, checked_at=now, next_check_at=now + timedelta(days=7))
        assert _overdue() == []

    def test_due_date_comes_from_latest_check(self) -> None:
        now = timezone.now()
        _check(1, checked_at=now - timedelta(days=30), next_check_at=now + timedelta(days=30))
        overdue = now - timedelta(days=1)
        latest = _check(1, checked_at=now - timedelta(days=8), next_check_at=overdue)
        assert _overdue() == [
            ("placement_link", 1, "link_alive", latest.checked_at, latest.next_check_at)
        ]

    def test_each_entity_and_check_type_separately(self) -> None:
        now = timezone.now()
        overdue = now - timedelta(days=1)
        _check(1, checked_at=now - timedelta(days=8), next_check_at=overdue)
        _check(2, checked_at=now, next_check_at=now + timedelta(days=7))
        # Другой тип проверки того же объекта — своя очередь.
        Check.objects.create(
            entity_type="placement_link",
            entity_id=2,
            check_type="indexation",
            status=CheckStatus.FAILED,
            checked_at=now - timedelta(days=8),
            next_check_at=overdue,
        )
        assert [(row[1], row[2]) for row in _overdue()] == [
            (1, "link_alive"),
            (2, "indexation"),
        ]
