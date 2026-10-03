"""Миграции Django дают ту же схему, что `schema.sql` (ADR-029).

Тест разворачивает `schema.sql` в отдельной схеме Postgres `ref` внутри
транзакции теста (после теста она откатывается) и сравнивает с тем, что
построили миграции в `public`: колонки, значения по умолчанию, индексы,
ограничения, внешние ключи, значения перечислений, текст представлений,
функций и триггеров.

Принятые расхождения из ADR-029 нормализуются перед сравнением:
`id` — identity вместо `bigserial`; `char(n)` — `varchar(n)`; имена,
`DEFERRABLE` и `ON DELETE` у внешних ключей; порядок колонок. В тексте
представлений те же два расхождения видны по-своему: `SELECT *` Postgres
разворачивает в порядке колонок таблицы, а у `char(n)` и `varchar(n)` по-разному
печатает приведения типов (`'US'::bpchar` и `(kp.country)::text`).

Списки таблиц и представлений растут с каждой задачей, которая их добавляет.
"""

import re
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.db.models import TextChoices

from apps.keywords.models import AnchorType
from apps.observability.models import CheckStatus, LlmStatus, Performer, TaskStatus
from apps.placements.models import PlacementStatus
from apps.sites.models import (
    AuditAuthor,
    AuditVerdict,
    MetricSource,
    PlacementType,
    ReviewGroup,
    SiteStatus,
    StatusSource,
    UploadKind,
    UploadStatus,
)

TABLES = [
    # E1-01
    "products",
    "sites",
    "product_sites",
    "site_metrics",
    "site_prices",
    "gray_scans",
    "site_audits",
    # E1-02
    "placements",
    "placement_links",
    "keywords",
    "keyword_positions",
    # E1-03
    "prompt_templates",
    "prompt_variants",
    "checks",
    "llm_calls",
    "task_runs",
    "api_usage",
    # E1-04
    "site_lists",
    "site_list_items",
    # E1-05
    "domain_settings",
    # E1-07
    "sellers",
    "site_notes",
    "exchange_rates",
    # E1-08
    "uploads",
    "upload_items",
    # E1-10
    "site_country_metrics",
    # E1-13
    "site_status_changes",
    "placement_status_changes",
    # E9-10
    "saved_filters",
]

VIEWS = [
    # E1-03
    "v_overdue_checks",
    # E1-05; v_article_funnel — с таблицами статей
    "v_keyword_coverage",
    "v_site_funnel",
    "v_monthly_spend",
    "v_link_health",
    "v_site_latest",
    "v_product_site_latest",
    # E1-07
    "v_site_offers",
    # E1-10
    "v_site_country_latest",
]

# Функции схемы: E1-07 — пересчёт в евро для представлений; E1-13 — строки
# истории статусов для триггеров.
FUNCTIONS = ["eur_rate", "log_site_status_change", "log_placement_status_change"]

# Триггеры схемы: E1-13 — история статусов (ADR-049).
TRIGGERS = [
    "product_sites_status_insert",
    "product_sites_status_update",
    "placements_status_insert",
    "placements_status_update",
]

ENUMS: dict[str, type[TextChoices]] = {
    "site_status": SiteStatus,
    "metric_source": MetricSource,
    "audit_verdict": AuditVerdict,
    "audit_author": AuditAuthor,
    "placement_status": PlacementStatus,
    "placement_type": PlacementType,
    "anchor_type": AnchorType,
    "check_status": CheckStatus,
    "performer": Performer,
    "llm_status": LlmStatus,
    "task_status": TaskStatus,
    "upload_kind": UploadKind,
    "upload_status": UploadStatus,
    "review_group": ReviewGroup,
    "status_source": StatusSource,
}

REF = "ref"
OURS = "public"


@pytest.fixture
def reference_schema(db: None) -> None:
    sql = (settings.BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA {REF}")
        # SET LOCAL действует до конца транзакции теста, дальше откат.
        cursor.execute(f"SET LOCAL search_path TO {REF}")
        cursor.execute(sql)
        cursor.execute(f"SET LOCAL search_path TO {OURS}")


def _rows(query: str, *params: Any) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


def _unqualify(text: str | None) -> str | None:
    if text is None:
        return None
    return text.replace(f"{REF}.", "").replace(f"{OURS}.", "")


def _columns(schema: str) -> dict[tuple[str, str], tuple[Any, ...]]:
    rows = _rows(
        """
        SELECT table_name, column_name, udt_name, character_maximum_length,
               numeric_precision, numeric_scale, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = ANY(%s)
        """,
        schema,
        TABLES,
    )
    result = {}
    for table, column, udt, length, precision, scale, nullable, default in rows:
        default = _unqualify(default)
        if column == "id":
            # bigserial в схеме, identity у Django — ведут себя одинаково.
            default = None
        if udt == "bpchar":
            # char(n) в схеме, varchar(n) у Django.
            udt = "varchar"
            default = default.replace("::bpchar", "::character varying") if default else None
        result[(table, column)] = (udt, length, precision, scale, nullable, default)
    return result


def _indexes(schema: str) -> dict[str, str | None]:
    rows = _rows(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = ANY(%s)",
        schema,
        TABLES,
    )
    return {name: _unqualify(definition) for name, definition in rows}


def _constraints(schema: str) -> dict[str, str]:
    # Первичные ключи, уникальность и CHECK — с именами из схемы.
    rows = _rows(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = %s AND t.relname = ANY(%s) AND c.contype IN ('p', 'u', 'c')
        """,
        schema,
        TABLES,
    )
    # CHECK со значением перечисления печатает тип со схемой: 'x'::ref.upload_kind.
    return {name: str(_unqualify(definition)) for name, definition in rows}


def _foreign_keys(schema: str) -> set[tuple[str, str, str, str]]:
    # Имена, DEFERRABLE и ON DELETE не сравниваем — ADR-029.
    rows = _rows(
        """
        SELECT t.relname, a.attname, rt.relname, ra.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        JOIN pg_class rt ON rt.oid = c.confrelid
        JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1]
        WHERE n.nspname = %s AND t.relname = ANY(%s) AND c.contype = 'f'
        """,
        schema,
        TABLES,
    )
    return {(table, column, ref_table, ref_column) for table, column, ref_table, ref_column in rows}


# Список колонок подзапроса: « SELECT x.id,\n    x.site_id,\n ... FROM».
_SUBQUERY_COLUMNS = re.compile(r"(\( SELECT )(.*?)(\n\s+FROM )", re.S)
# Колонка в скобках с приведением: «(kp.country)::text».
_CAST_COLUMN = re.compile(r"\((\w+\.\w+)\)::text")


def _sort_columns(match: re.Match[str]) -> str:
    columns = sorted(column.strip() for column in match.group(2).split(","))
    return match.group(1) + ", ".join(columns) + match.group(3)


def _normalize_view(text: str | None) -> str | None:
    """Убирает из текста представления расхождения ADR-029, одинаково для обеих схем.

    Типы колонок сверяет `test_columns`, поэтому разница `char` и `varchar`
    в приведениях здесь не нужна.
    """
    if text is None:
        return None
    text = _SUBQUERY_COLUMNS.sub(_sort_columns, text)
    text = text.replace("::bpchar", "::text").replace("::character varying", "::text")
    return _CAST_COLUMN.sub(r"\1", text)


def _views(schema: str) -> dict[str, str | None]:
    # Текст представления в том виде, как его хранит Postgres: пробелы и
    # переносы из исходного SQL не влияют, влияет смысл запроса.
    rows = _rows(
        """
        SELECT c.relname, pg_get_viewdef(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind = 'v'
        """,
        schema,
        VIEWS,
    )
    return {name: _normalize_view(_unqualify(definition)) for name, definition in rows}


def _functions(schema: str) -> dict[str, str | None]:
    rows = _rows(
        """
        SELECT p.proname, pg_get_functiondef(p.oid)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = %s AND p.proname = ANY(%s)
        """,
        schema,
        FUNCTIONS,
    )
    # Тело функции Postgres хранит как написано — с отступами миграции.
    # Сравниваем смысл: пробелы и переносы сводим к одному пробелу.
    return {name: " ".join(str(_unqualify(definition)).split()) for name, definition in rows}


def _triggers(schema: str) -> dict[str, str | None]:
    # Внутренние триггеры внешних ключей — не наши, у них tgisinternal.
    rows = _rows(
        """
        SELECT t.tgname, pg_get_triggerdef(t.oid)
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND NOT t.tgisinternal
        """,
        schema,
    )
    return {name: _unqualify(definition) for name, definition in rows}


def _enum_labels(schema: str) -> dict[str, list[str]]:
    rows = _rows(
        """
        SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = %s AND t.typname = ANY(%s)
        GROUP BY t.typname
        """,
        schema,
        list(ENUMS),
    )
    return {name: list(labels) for name, labels in rows}


@pytest.mark.usefixtures("reference_schema")
class TestSchemaParity:
    def test_reference_has_all_tables(self) -> None:
        # Защита от опечатки в TABLES: иначе сравнение пустого с пустым пройдёт.
        tables = {table for table, _ in _columns(REF)}
        assert tables == set(TABLES)

    def test_reference_has_all_views(self) -> None:
        assert set(_views(REF)) == set(VIEWS)

    def test_columns(self) -> None:
        assert _columns(OURS) == _columns(REF)

    def test_indexes(self) -> None:
        # Лишний индекс (например, автоиндекс Django на внешний ключ) тоже ошибка.
        assert _indexes(OURS) == _indexes(REF)

    def test_constraints(self) -> None:
        assert _constraints(OURS) == _constraints(REF)

    def test_foreign_keys(self) -> None:
        assert _foreign_keys(OURS) == _foreign_keys(REF)

    def test_views(self) -> None:
        assert _views(OURS) == _views(REF)

    def test_reference_has_all_functions(self) -> None:
        assert set(_functions(REF)) == set(FUNCTIONS)

    def test_functions(self) -> None:
        assert _functions(OURS) == _functions(REF)

    def test_reference_has_all_triggers(self) -> None:
        assert set(_triggers(REF)) == set(TRIGGERS)

    def test_triggers(self) -> None:
        # Лишний триггер в базе — тоже ошибка: его нет в схеме.
        assert _triggers(OURS) == _triggers(REF)

    def test_enum_types(self) -> None:
        assert _enum_labels(OURS) == _enum_labels(REF)


@pytest.mark.django_db
@pytest.mark.parametrize(("enum_type", "choices"), ENUMS.items())
def test_enum_choices_match_database(enum_type: str, choices: type[TextChoices]) -> None:
    # Значения в модели и в типе Postgres совпадают, включая порядок.
    assert _enum_labels(OURS)[enum_type] == list(choices.values)
