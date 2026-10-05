"""Отмена загрузки целиком — по её журналу (ADR-060).

Запись загрузки идёт с журналом (`upload_changes`, триггер `log_upload_change`):
что вставлено, что поменяно и удалено — со строкой до изменения. Отмена в одной
транзакции:

1. строки, которые загрузка поменяла или удалила, возвращаются такими, какими
   были до неё (по первой записи журнала о строке);
2. строки, которые она вставила, удаляются — вместе со всем, что на них успели
   сослать после (`config.deletion`: размещение, созданное вручную на площадке
   из загрузки, уйдёт с площадкой — подтверждение это показывает);
3. сотрудники, которых завела загрузка, удаляются, только если на них больше
   ничего не ссылается;
4. строки истории статусов, которые отмена записала сама, возвращая статусы,
   удаляются: отмена — «как будто загрузки не было»;
5. удаляется сама загрузка, её строки разбора и журнал, после фиксации — файл.

Если строки этой загрузки потом трогала более поздняя загрузка, отмена
запрещена: сначала отменяют ту — иначе её изменения пропали бы молча.

Загрузка, записанная до журнала (`journaled` пусто), отменяется только как
запись о загрузке: её данные остаются в базе.
"""

import json
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, models, transaction

from apps.sites.models import Upload, UploadStatus
from config import deletion

# Отмена по журналу пишет историю статусов от своего run_id — потом её стирает.
_HISTORY_TABLES = ("site_status_changes", "placement_status_changes")


class Blocked(Exception):
    """Строки загрузки потом меняла более поздняя загрузка — сначала отменить её."""

    def __init__(self, later: list[Upload]) -> None:
        self.later = later
        super().__init__(", ".join(f"№{u.pk}" for u in later))


@dataclass
class Report:
    """Что сделает (или сделала) отмена — для подтверждения и сообщения."""

    upload: Upload
    journaled: bool
    plan: deletion.Plan = field(default_factory=deletion.Plan)
    restored: dict[str, int] = field(default_factory=dict)  # «Площадки продуктов» → строк
    after: dict[str, int] = field(default_factory=dict)  # удалится созданное после загрузки
    kept_users: int = 0
    later: list[Upload] = field(default_factory=list)

    def lines(self) -> list[str]:
        if not self.journaled:
            return [
                "Загрузка записана до журнала изменений: удалится только она сама и файл, "
                "записанное ею в базе останется."
            ]
        result = [
            f"Удалится — {line.text.lower()}: {line.count}"
            for line in self.plan.lines()
            if not line.cleared and line.text != _plural(Upload)
        ]
        result += [
            f"Вернётся как было — {name.lower()}: {count}" for name, count in self.restored.items()
        ]
        result += [
            f"В том числе созданное после загрузки — {name.lower()}: {count}"
            for name, count in self.after.items()
        ]
        result += [f"{line.text}: {line.count}" for line in self.plan.lines() if line.cleared]
        if self.kept_users:
            result.append(f"Сотрудники, на которых уже ссылаются, останутся: {self.kept_users}")
        return result or ["Загрузка ничего не меняла в базе — удалится только она сама."]


def later_uploads(upload: Upload) -> list[Upload]:
    """Более поздние загрузки, которые трогали те же строки."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT later.upload_id
            FROM upload_changes own
            JOIN upload_changes later
              ON later.table_name = own.table_name
             AND later.row_id = own.row_id
             AND later.upload_id > own.upload_id
            WHERE own.upload_id = %s
            """,
            [upload.pk],
        )
        ids = [row[0] for row in cursor.fetchall()]
    return list(Upload.objects.filter(pk__in=ids).order_by("-pk"))


def preview(upload: Upload) -> Report:
    """Что сделает отмена — прогоном в транзакции, которая откатывается."""
    later = later_uploads(upload)
    if later:
        return Report(upload, upload.journaled, later=later)
    with _rolled_back():
        return _run(upload)


def undo(upload: Upload) -> Report:
    """Отменяет загрузку. Нельзя — `Blocked` со списком более поздних загрузок."""
    with transaction.atomic():
        locked = Upload.objects.select_for_update().get(pk=upload.pk)
        if locked.status == UploadStatus.WRITING:
            raise Blocked([])
        later = later_uploads(locked)
        if later:
            raise Blocked(later)
        report = _run(locked)
        path = Path(settings.UPLOADS_DIR) / locked.file_path
        transaction.on_commit(lambda: path.unlink(missing_ok=True))
    return report


# --- Отмена ---


class _Rollback(Exception):
    pass


@contextmanager
def _rolled_back() -> Iterator[None]:
    try:
        with transaction.atomic():
            yield
            raise _Rollback
    except _Rollback:
        pass


def _run(upload: Upload) -> Report:
    report = Report(upload, upload.journaled)
    run_id = str(uuid.uuid4())
    with connection.cursor() as cursor:
        # Отмена сама в журнал не пишет; история статусов — от её run_id.
        cursor.execute(
            "SELECT set_config('seo.upload_id', '', true),"
            " set_config('seo.change_source', '', true),"
            " set_config('seo.change_run_id', %s, true)",
            [run_id],
        )
        inserted, before = _journal(cursor, upload.pk)
        for table, rows in before.items():
            _restore(cursor, table, rows)
            report.restored[_plural(_model(table))] = len(rows)

    users = inserted.pop(User._meta.db_table, [])
    roots: dict[deletion.Model, list[int]] = {_model(t): ids for t, ids in inserted.items()}
    roots[Upload] = [upload.pk]
    plan = deletion.collect(roots)
    for model, ids in plan.deleted.items():
        extra = len(ids - set(roots.get(model, ())))
        if extra:
            report.after[_plural(model)] = extra
    keep = _referenced_users(users, plan)
    report.kept_users = len(keep)
    removable = [pk for pk in users if pk not in keep]
    if removable:
        plan.deleted.setdefault(User, set()).update(removable)
    deletion.execute(plan)
    report.plan = plan

    with connection.cursor() as cursor:
        for table in _HISTORY_TABLES:
            cursor.execute(f"DELETE FROM {table} WHERE run_id = %s", [run_id])
    return report


def _journal(cursor: Any, upload_id: int) -> tuple[dict[str, list[int]], dict[str, list[Any]]]:
    """Первая запись журнала о каждой строке: вставлена — или какой была до загрузки."""
    cursor.execute(
        """
        SELECT DISTINCT ON (table_name, row_id) table_name, row_id, op, before
        FROM upload_changes
        WHERE upload_id = %s
        ORDER BY table_name, row_id, id
        """,
        [upload_id],
    )
    inserted: dict[str, list[int]] = defaultdict(list)
    before: dict[str, list[Any]] = defaultdict(list)
    for table, row_id, op, row in cursor.fetchall():
        if op == "I":
            inserted[table].append(row_id)
        else:
            before[table].append(json.loads(row) if isinstance(row, str) else row)
    return dict(inserted), dict(before)


def _restore(cursor: Any, table: str, rows: list[Any]) -> None:
    """Строки — какими были до загрузки: есть в таблице — правка, удалены — вставка."""
    quote = connection.ops.quote_name
    columns = [c for c in _columns(cursor, table) if c != "id"]
    target = ", ".join(quote(c) for c in columns)
    source = ", ".join(f"r.{quote(c)}" for c in columns)
    name = quote(table)
    data = json.dumps(rows)
    cursor.execute(
        f"UPDATE {name} AS t SET ({target}) = ({source})"
        f" FROM jsonb_populate_recordset(NULL::{name}, %s::jsonb) AS r WHERE t.id = r.id",
        [data],
    )
    cursor.execute(
        f"INSERT INTO {name} SELECT r.* FROM jsonb_populate_recordset(NULL::{name}, %s::jsonb) AS r"
        f" WHERE NOT EXISTS (SELECT 1 FROM {name} AS x WHERE x.id = r.id)",
        [data],
    )


def _columns(cursor: Any, table: str) -> list[str]:
    cursor.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        [table],
    )
    return [row[0] for row in cursor.fetchall()]


def _referenced_users(users: list[int], plan: deletion.Plan) -> set[int]:
    """Сотрудники загрузки, на которых ссылается что-то, что остаётся в базе."""
    keep: set[int] = set()
    for pk in users:
        own = deletion.collect({User: [pk]})
        staying = any(
            ids - plan.deleted.get(model, set())
            for model, ids in own.deleted.items()
            if model is not User
        ) or any(ids - plan.deleted.get(model, set()) for (model, _), ids in own.cleared.items())
        if staying:
            keep.add(pk)
    return keep


def _model(table: str) -> type[models.Model]:
    for model in apps.get_models():
        if model._meta.db_table == table:
            return model
    raise LookupError(f"Нет модели для таблицы {table}")


def _plural(model: type[models.Model]) -> str:
    return str(model._meta.verbose_name_plural).capitalize()
