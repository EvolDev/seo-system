"""Удаление записи вместе со всем, что на неё ссылается (ADR-060).

До ADR-060 удаление было отключено везде (ADR-008). Теперь удалить можно
любую запись, а подтверждение заранее показывает, что уйдёт вместе с ней:

- строки, которые без записи не живут (обязательная ссылка на неё), —
  удаляются следом, и так по цепочке: продавец → его цены → строки разбора;
- строки, у которых ссылка необязательная, остаются, ссылка в них
  очищается: удалили продавца — размещения остаются «без продавца».

`on_delete=PROTECT` у моделей (ADR-008) здесь не мешает: план собирается
обходом связей, а удаляет его один SQL-запрос на таблицу. Внешние ключи в базе
отложенные (DEFERRABLE INITIALLY DEFERRED), поэтому порядок таблиц не важен;
в конце `SET CONSTRAINTS ALL IMMEDIATE` — пропущенная ссылка станет ошибкой
сразу, а не на фиксации транзакции; потом проверка снова отложенная.

Связи без внешнего ключа (журнал проверок `checks`: тип записи и её id)
приложение регистрирует само — `register_dependents` в `AppConfig.ready()`.
"""

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from django.db import connection, models, transaction

CHUNK = 5000

type Model = type[models.Model]
# Зависимые без внешнего ключа: по id записей — модель и id строк, которые удалить.
type Dependents = Callable[[list[int]], tuple[Model, Iterable[int]]]

_dependents: dict[Model, list[Dependents]] = defaultdict(list)


def register_dependents(model: Model, find: Dependents) -> None:
    """Строки, которые ссылаются на `model` без внешнего ключа, — удаляются вместе с ней."""
    _dependents[model].append(find)


@dataclass(frozen=True)
class Line:
    """Строка подтверждения: «Размещения — 455» или «без продавца останутся размещения — 3»."""

    text: str
    count: int
    cleared: bool = False


@dataclass
class Plan:
    deleted: dict[Model, set[int]] = field(default_factory=dict)
    # (модель, колонка внешнего ключа) → строки, где ссылку очистить.
    cleared: dict[tuple[Model, str], set[int]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not any(self.deleted.values())

    def lines(self) -> list[Line]:
        result = [Line(_plural(model), len(ids)) for model, ids in self.deleted.items() if ids]
        for (model, attname), ids in self.cleared.items():
            name = _field_name(model, attname)
            result.append(Line(f"{_plural(model)} останутся без «{name}»", len(ids), True))
        return result

    def count(self, model: Model) -> int:
        return len(self.deleted.get(model, ()))


def collect(roots: Mapping[Model, Iterable[int]]) -> Plan:
    """Что удалится и где очистится ссылка, если удалить `roots`. Базу не меняет."""
    deleted: dict[Model, set[int]] = {}
    nullable: dict[tuple[Model, str], set[int]] = defaultdict(set)
    queue: deque[tuple[Model, list[int]]] = deque()

    def add(model: Model, ids: Iterable[int]) -> None:
        known = deleted.setdefault(model, set())
        new = [pk for pk in ids if pk not in known]
        if new:
            known.update(new)
            queue.append((model, new))

    for model, ids in roots.items():
        add(model, ids)
    while queue:
        model, ids = queue.popleft()
        for relation in _reverse_relations(model):
            related: Model = relation.related_model
            fk = relation.field
            for chunk in _chunks(ids):
                rows = related._base_manager.filter(**{f"{fk.attname}__in": chunk})
                found = list(rows.values_list("pk", flat=True))
                if fk.null:
                    nullable[(related, fk.attname)].update(found)
                else:
                    add(related, found)
        for find in _dependents.get(model, ()):
            for chunk in _chunks(ids):
                dependent, extra = find(chunk)
                add(dependent, extra)
    cleared = {
        key: ids - deleted.get(key[0], set())
        for key, ids in nullable.items()
        if ids - deleted.get(key[0], set())
    }
    return Plan(deleted, cleared)


def execute(plan: Plan) -> None:
    """Очищает ссылки и удаляет строки плана в одной транзакции."""
    with transaction.atomic(), connection.cursor() as cursor:
        for (model, attname), ids in plan.cleared.items():
            for chunk in _chunks(sorted(ids)):
                model._base_manager.filter(pk__in=chunk).update(**{attname: None})
        quote = connection.ops.quote_name
        for model, ids in plan.deleted.items():
            table = quote(model._meta.db_table)
            column = model._meta.pk.column if model._meta.pk else None
            pk = quote(column or "id")
            for chunk in _chunks(sorted(ids)):
                cursor.execute(f"DELETE FROM {table} WHERE {pk} = ANY(%s)", [chunk])
        # Проверить ссылки сейчас — и вернуть отложенную проверку: режим живёт до
        # конца всей транзакции, а снаружи (страница подтверждения, отмена загрузки)
        # она ещё идёт.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def delete(roots: Mapping[Model, Iterable[int]]) -> Plan:
    """Собрать и выполнить: вернёт план — что удалено."""
    with transaction.atomic():
        plan = collect(roots)
        execute(plan)
    return plan


def _reverse_relations(model: Model) -> list[Any]:
    """Связи «на эту модель ссылается другая» — и скрытые (related_name="+")."""
    return [
        relation
        for relation in model._meta.get_fields(include_hidden=True)
        if (relation.one_to_many or relation.one_to_one)
        and relation.auto_created
        and not relation.concrete
        and relation.related_model is not None
        and relation.related_model._meta.managed
    ]


def _plural(model: Model) -> str:
    return str(model._meta.verbose_name_plural).capitalize()


def _field_name(model: Model, attname: str) -> str:
    for model_field in model._meta.concrete_fields:
        if model_field.attname == attname:
            return str(model_field.verbose_name)
    return attname


def _chunks[T](values: Iterable[T]) -> list[list[T]]:
    items = list(values)
    return [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)]
