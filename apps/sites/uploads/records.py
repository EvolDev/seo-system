"""Строки файла → записи о площадках: цены по услугам, метрики, прочие данные.

Базы здесь нет. Строка, которая не разобралась, — в «Ошибки» с номером и
причиной; одна битая ячейка не останавливает загрузку. Дубли домена в
файле сливаются: цена каждой услуги — меньшая из строк, все строки — в
отчёт (у `aijourn.com` в `2.xlsx` две цены).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial

from apps.sites.models import PlacementType
from apps.sites.uploads import values
from apps.sites.uploads.columns import Field
from apps.sites.uploads.files import Column, Row, Table, is_blank, show

METRIC_FIELDS = {
    Field.DR: "dr",
    Field.TRAFFIC: "organic_traffic",
    Field.KEYWORDS: "total_keywords",
}


@dataclass
class Record:
    """Площадка из файла. Имена полей цен и метрик — как в моделях."""

    line: int
    domain: str
    source_value: str | None = None  # адрес из файла, если там не просто домен
    offers: dict[PlacementType, int] = field(default_factory=dict)  # услуга → центы
    gray_cents: int | None = None
    writing_cents: int | None = None
    announce_cents: int | None = None
    metrics: dict[str, int | None] = field(default_factory=dict)
    link_type: str | None = None
    note: str | None = None
    extra: dict[str, str] = field(default_factory=dict)  # заголовок → значение
    card: dict[str, object] | None = None  # факты карточки — только из каталога

    def price_fields(self) -> dict[str, int | None]:
        return {
            "gray_cents": self.gray_cents,
            "writing_cents": self.writing_cents,
            "announce_cents": self.announce_cents,
        }


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


@dataclass(frozen=True)
class Duplicate:
    domain: str
    lines: tuple[int, ...]
    prices: tuple[str, ...]  # «публикация 180» — как в файле, по строкам


@dataclass
class Parsed:
    records: list[Record]
    errors: list[RowError]
    duplicates: list[Duplicate]


def parse_price_list(table: Table, mapping: Mapping[str, Field], currency: str) -> Parsed:
    """Прайс продавца по разметке. Валюта — одна на файл, ею помечены все цены."""
    columns = {
        mapping[c.key]: c
        for c in table.columns
        if mapping.get(c.key) not in (None, Field.EXTRA, Field.SKIP)
    }
    extra_columns = [
        c for c in table.columns if mapping.get(c.key, Field.EXTRA) == Field.EXTRA and c.filled
    ]
    records: list[Record] = []
    errors: list[RowError] = []
    for row in table.rows:
        try:
            records.append(_price_row(row, columns, extra_columns))
        except ValueError as error:
            errors.append(RowError(row.line, str(error)))
    merged, duplicates = merge_duplicates(records)
    return Parsed(merged, errors, duplicates)


def merge_duplicates(records: Sequence[Record]) -> tuple[list[Record], list[Duplicate]]:
    """Одна запись на домен. Цена услуги — меньшая; остальное — из первой строки."""
    groups: dict[str, list[Record]] = {}
    for record in records:
        groups.setdefault(record.domain, []).append(record)
    merged: list[Record] = []
    duplicates: list[Duplicate] = []
    for domain, group in groups.items():
        first = group[0]
        if len(group) == 1:
            merged.append(first)
            continue
        offers: dict[PlacementType, int] = {}
        for record in group:
            for service, cents in record.offers.items():
                offers[service] = min(cents, offers.get(service, cents))
        merged.append(replace(first, offers=offers))
        duplicates.append(
            Duplicate(
                domain=domain,
                lines=tuple(r.line for r in group),
                prices=tuple(_offers_text(r) for r in group),
            )
        )
    return merged, duplicates


def _price_row(row: Row, columns: Mapping[Field, Column], extra: Sequence[Column]) -> Record:
    def cell(f: Field) -> object:
        column = columns.get(f)
        return row.get(column.index) if column is not None else None

    domain, is_url = values.parse_domain(cell(Field.DOMAIN))
    record = Record(line=row.line, domain=domain)
    if is_url:
        record.source_value = show(cell(Field.DOMAIN))

    money = _reader(lambda f: values.parse_money(cell(f)), columns)
    for f, fixed in (
        (Field.GUEST_POST, PlacementType.GUEST_POST),
        (Field.LINK_INSERTION, PlacementType.LINK_INSERTION),
    ):
        cents = money(f)
        if cents is not None:
            record.offers[fixed] = cents
    if Field.PRICE in columns:
        cents = money(Field.PRICE)
        service = _read(lambda: values.parse_service(cell(Field.SERVICE)), columns[Field.SERVICE])
        if cents is not None:
            if service is None:
                raise ValueError(
                    f"{domain}: не указана услуга в колонке «{columns[Field.SERVICE].header}»"
                )
            # Публикация в приоритете: та же услуга из другой колонки — меньшая цена.
            record.offers[service] = min(cents, record.offers.get(service, cents))
        if values.is_both(cell(Field.SERVICE)):
            # «Both» записываем публикацией; сама пометка не теряется (Q21).
            record.extra[columns[Field.SERVICE].header] = show(cell(Field.SERVICE))
    if not record.offers:
        raise ValueError(f"{domain}: нет цены")

    record.gray_cents = money(Field.GRAY)
    record.writing_cents = _read(
        lambda: values.parse_money(cell(Field.WRITING), free_is_zero=True),
        columns.get(Field.WRITING),
    )
    record.announce_cents = _read(
        lambda: values.parse_money(cell(Field.ANNOUNCE), free_is_zero=True),
        columns.get(Field.ANNOUNCE),
    )
    for f, name in METRIC_FIELDS.items():
        if f in columns:
            record.metrics[name] = _read(partial(_number, cell(f)), columns[f])
    if Field.LINK_TYPE in columns:
        record.link_type = _read(
            lambda: values.parse_link_type(cell(Field.LINK_TYPE)), columns[Field.LINK_TYPE]
        )
    note = cell(Field.NOTE)
    record.note = None if is_blank(note) else show(note)
    for column in extra:
        value = row.get(column.index)
        if not is_blank(value):
            record.extra[column.header] = show(value)
    return record


def _number(value: object) -> int | None:
    return values.parse_number(value)


def _reader(
    parse: Callable[[Field], int | None], columns: Mapping[Field, Column]
) -> Callable[[Field], int | None]:
    def read(f: Field) -> int | None:
        if f not in columns:
            return None
        return _read(lambda: parse(f), columns[f])

    return read


def _read[T](parse: Callable[[], T], column: Column | None) -> T:
    """Значение ячейки; ошибка разбора — с названием колонки."""
    try:
        return parse()
    except ValueError as error:
        name = f"«{column.header}»: " if column is not None else ""
        raise ValueError(f"{name}{error}") from None


def _offers_text(record: Record) -> str:
    labels = {PlacementType.GUEST_POST: "публикация", PlacementType.LINK_INSERTION: "вставка"}
    parts = [f"{labels[s]} {cents / 100:g}" for s, cents in record.offers.items()]
    return ", ".join(parts) or "без цены"
