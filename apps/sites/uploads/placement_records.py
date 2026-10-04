"""Строки файла размещений → записи: площадка, статья, ссылки, цены, отметки (E1-09).

Базы здесь нет. Что не разобралось:
- строку без площадки, с непонятным статусом или с адресом статьи не на
  площадке не записываем — она в «Ошибки» с номером и причиной, как у
  импорта таблицы (маппинг §1.5–1.6);
- непонятное в остальных ячейках строку не останавливает: дата и отметка
  индексации остаются пустыми, ссылка без адреса не пишется — всё это в
  сводке до записи, с номером строки.

Анкор и ссылка, перепутанные местами, исправляются: ссылка — то, что похоже на
адрес, а из двух адресов — тот, что на домене продукта. Комментарий «Link
insert» — это тип размещения, а не заметка. `#Н/Д` из формул Google Таблиц —
пустая ячейка.
"""

import datetime as dt
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from apps.placements.indexation import normalize_url
from apps.placements.models import PlacementStatus
from apps.sites.domains import normalize_domain
from apps.sites.importing.values import is_url_on_domain
from apps.sites.models import PlacementType
from apps.sites.uploads import values
from apps.sites.uploads.files import Column, Grouped, Row, Table, is_blank, show
from apps.sites.uploads.placement_columns import LINK_PAIRS, PField

# Ошибка формулы в выгрузке Google Таблиц — значения нет.
_NA = {"#н/д", "#n/a", "н/д", "#value!", "#ref!"}

_STATUSES: dict[str, PlacementStatus] = {
    **{label.lower(): PlacementStatus(value) for value, label in PlacementStatus.choices},
    "размещено": PlacementStatus.PUBLISHED,
    "размещена": PlacementStatus.PUBLISHED,
    "опубликована": PlacementStatus.PUBLISHED,
    "published": PlacementStatus.PUBLISHED,
    "placed": PlacementStatus.PUBLISHED,
    "live": PlacementStatus.PUBLISHED,
    "done": PlacementStatus.PUBLISHED,
    "заявка": PlacementStatus.ORDERED,
    "ordered": PlacementStatus.ORDERED,
    "in progress": PlacementStatus.WRITING,
    "writing": PlacementStatus.WRITING,
    "review": PlacementStatus.REVIEW,
    "planned": PlacementStatus.PLANNED,
    "rejected": PlacementStatus.REJECTED,
    "cancelled": PlacementStatus.CANCELLED,
    "canceled": PlacementStatus.CANCELLED,
}
_YES = {"да", "yes", "y", "true", "1", "+", "в индексе", "indexed"}
_NO = {"нет", "no", "n", "false", "0", "-", "не в индексе", "not indexed"}
_HEADER_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")
# Число Excel вместо даты: 45 000 — 2023 год, 60 000 — 2064-й.
_EXCEL_DAYS = (30_000, 60_000)
_EXCEL_EPOCH = dt.date(1899, 12, 30)
_DOMAIN_LIKE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/?#]\S*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class LinkData:
    index: int  # номер ссылки в статье
    anchor: str
    target_url: str


@dataclass(frozen=True)
class IndexMark:
    """Отметка индексации человеком: в индексе или нет на дату."""

    day: dt.date | None  # пусто — на дату файла
    indexed: bool
    header: str


@dataclass(frozen=True)
class Issue:
    """Непонятное в строке, которая всё равно запишется: в сводку до записи."""

    line: int
    domain: str
    message: str


@dataclass
class PlacementRecord:
    """Размещение из строки файла. Цены — центы в валюте файла."""

    line: int
    domain: str
    source_value: str | None = None  # адрес из колонки площадки, если там не просто домен
    article_url: str | None = None
    published_on: dt.date | None = None
    status: PlacementStatus | None = None  # пусто — в файле нет статуса
    placement_type: PlacementType | None = None
    seller: str | None = None
    employee: str | None = None
    links: list[LinkData] = field(default_factory=list)
    paid_cents: int | None = None
    price_cents: int | None = None
    writing_cents: int | None = None
    announce_cents: int | None = None
    marks: list[IndexMark] = field(default_factory=list)
    note: str | None = None
    extra: dict[str, str] = field(default_factory=dict)  # заголовок → значение

    @property
    def offer_cents(self) -> int | None:
        """Цена размещения для предложения продавца: своя колонка, иначе весь итог."""
        return self.price_cents if self.price_cents is not None else self.paid_cents


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


@dataclass
class ParsedPlacements:
    records: list[PlacementRecord]
    errors: list[RowError] = field(default_factory=list)
    issues: dict[str, list[Issue]] = field(default_factory=dict)  # вид → строки
    duplicates: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)

    def issue(self, kind: str, record: PlacementRecord, message: str) -> None:
        self.issues.setdefault(kind, []).append(Issue(record.line, record.domain, message))


# Виды непонятного — ключи сводки.
BAD_DATE = "bad_dates"
BAD_INDEX = "bad_index"
LINK_NOT_URL = "links_not_url"
LINK_SWAPPED = "links_swapped"
LINK_FOREIGN = "links_foreign"
BAD_SERVICE = "bad_service"
PRICE_FIXED = "prices_fixed"


def parse_placements(
    table: Table, mapping: Mapping[str, PField], *, product_domain: str
) -> ParsedPlacements:
    """Файл размещений по разметке. Валюта — одна на файл, её знает загрузка."""
    columns = {
        mapping[c.key]: c
        for c in table.columns
        if mapping.get(c.key) not in (None, PField.EXTRA, PField.SKIP, PField.INDEXED)
    }
    marks = [c for c in table.columns if mapping.get(c.key) == PField.INDEXED]
    extra = [
        c for c in table.columns if mapping.get(c.key, PField.EXTRA) == PField.EXTRA and c.filled
    ]
    parsed = ParsedPlacements(records=[])
    seen: dict[tuple[str, str], list[int]] = {}
    for row in table.rows:
        try:
            record = _row(row, columns, marks, extra, product_domain, parsed)
        except ValueError as error:
            parsed.errors.append(RowError(row.line, str(error)))
            continue
        key = (record.domain, normalize_url(record.article_url) if record.article_url else "")
        seen.setdefault(key, []).append(record.line)
        if len(seen[key]) == 1:
            parsed.records.append(record)
    parsed.duplicates = [
        (domain, tuple(lines)) for (domain, _), lines in seen.items() if len(lines) > 1
    ]
    return parsed


def header_day(header: str) -> dt.date | None:
    """Дата отметки из заголовка: «Индексация 02.09.26» → 02.09.2026."""
    match = _HEADER_DATE.search(header)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_day(value: object) -> dt.date | None:
    """Дата из ячейки: дата Excel, число Excel, `дд.мм.гггг`, `д.м.гг`, `гггг-мм-дд`."""
    if _empty(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        if _EXCEL_DAYS[0] <= value <= _EXCEL_DAYS[1]:
            return _EXCEL_EPOCH + dt.timedelta(days=int(value))
        raise ValueError(f"дата «{show(value)}» не разобрана")
    text = show(value)
    for pattern in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"дата «{text}» не разобрана")


def parse_status(value: object) -> PlacementStatus | None:
    if _empty(value):
        return None
    text = " ".join(show(value).lower().split())
    if text in _STATUSES:
        return _STATUSES[text]
    raise ValueError(f"статус «{show(value)}» не распознан")


def parse_indexed(value: object) -> bool | None:
    if _empty(value):
        return None
    if isinstance(value, bool):
        return value
    text = " ".join(show(value).lower().split())
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise ValueError(f"индексация «{show(value)}» — ни «да», ни «нет»")


def is_link_like(value: str) -> bool:
    """Похоже на адрес: со схемой или как домен — `clideo.com/tools`."""
    return value.lower().startswith(("http://", "https://")) or bool(_DOMAIN_LIKE.match(value))


def is_on_domain(url: str, domain: str) -> bool:
    try:
        host = normalize_domain(url)
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


# --- Одна строка ---


def _row(
    row: Row,
    columns: Mapping[PField, Column],
    marks: Sequence[Column],
    extra: Sequence[Column],
    product_domain: str,
    parsed: ParsedPlacements,
) -> PlacementRecord:
    def cell(f: PField) -> object:
        column = columns.get(f)
        value = row.get(column.index) if column is not None else None
        return None if _empty(value) else value

    domain, is_url = values.parse_domain(cell(PField.DOMAIN))
    record = PlacementRecord(line=row.line, domain=domain)
    if is_url:
        record.source_value = show(cell(PField.DOMAIN))
    record.status = _read(lambda: parse_status(cell(PField.STATUS)), columns.get(PField.STATUS))
    record.article_url = _article(cell(PField.ARTICLE_URL), domain, columns)
    record.seller = _text(cell(PField.SELLER))
    record.employee = _text(cell(PField.EMPLOYEE))

    try:
        record.published_on = parse_day(cell(PField.PUBLISHED))
    except ValueError as error:
        parsed.issue(BAD_DATE, record, f"{error} — оставлена пустой")

    money = _money_reader(cell, columns, record, parsed)
    record.paid_cents = money(PField.PAID)
    record.price_cents = money(PField.PRICE)
    record.writing_cents = money(PField.WRITING)
    record.announce_cents = money(PField.ANNOUNCE)

    service = cell(PField.SERVICE)
    if service is not None:
        try:
            record.placement_type = values.parse_service(service)
        except ValueError as error:
            parsed.issue(BAD_SERVICE, record, f"{error} — тип не записан")
    note = _text(cell(PField.NOTE))
    if note is not None:
        # «Link insert» в комментарии — тип размещения, заметкой он не нужен.
        typed = _service_or_none(note)
        if typed is not None and record.placement_type is None:
            record.placement_type = typed
        elif typed is None:
            record.note = note

    for index, anchor_field, link_field in LINK_PAIRS:
        if link_field in columns or anchor_field in columns:
            _link(
                record,
                index,
                _text(cell(anchor_field)),
                _text(cell(link_field)),
                product_domain,
                parsed,
            )

    for column in marks:
        value = row.get(column.index)
        try:
            indexed = parse_indexed(value)
        except ValueError as error:
            parsed.issue(BAD_INDEX, record, f"«{column.header}»: {error}")
            continue
        if indexed is not None:
            record.marks.append(IndexMark(header_day(column.header), indexed, column.header))

    for column in extra:
        value = row.get(column.index)
        if not _empty(value):
            record.extra[column.header] = show(value)
    return record


def _article(value: object, domain: str, columns: Mapping[PField, Column]) -> str | None:
    if value is None:
        return None
    url = show(value)
    if not url.lower().startswith(("http://", "https://")) and is_link_like(url):
        url = f"https://{url}"
    if not is_url_on_domain(url, domain):
        header = columns[PField.ARTICLE_URL].header
        raise ValueError(f"«{header}»: «{show(value)}» — не адрес статьи на {domain}")
    return url


def _link(
    record: PlacementRecord,
    index: int,
    anchor: str | None,
    link: str | None,
    product_domain: str,
    parsed: ParsedPlacements,
) -> None:
    if anchor is None and link is None:
        return
    if _link_score(anchor, product_domain) > _link_score(link, product_domain):
        anchor, link = link, anchor
        parsed.issue(
            LINK_SWAPPED, record, f"ссылка {index}: анкор и ссылка были перепутаны местами"
        )
    if link is None or not is_link_like(link):
        shown = f"«{link}»" if link else "пусто"
        parsed.issue(
            LINK_NOT_URL, record, f"ссылка {index}: вместо адреса {shown} — ссылка не записана"
        )
        return
    if not link.lower().startswith(("http://", "https://")):
        link = f"https://{link}"
    if not is_on_domain(link, product_domain):
        parsed.issue(LINK_FOREIGN, record, f"ссылка {index} ведёт не на {product_domain}: {link}")
    record.links.append(LinkData(index, anchor or "", link))


def _link_score(value: str | None, product_domain: str) -> int:
    """Насколько значение похоже на ссылку: адрес продукта со схемой — больше всех."""
    if value is None or not is_link_like(value):
        return 0
    if not value.lower().startswith(("http://", "https://")):
        return 1
    return 3 if is_on_domain(value, product_domain) else 2


def _money_reader(
    cell: Callable[[PField], object],
    columns: Mapping[PField, Column],
    record: PlacementRecord,
    parsed: ParsedPlacements,
) -> Callable[[PField], int | None]:
    def read(f: PField) -> int | None:
        if f not in columns:
            return None
        value = cell(f)
        if isinstance(value, Grouped):
            # «197.393» в таблице — центы с лишней цифрой, а не 197 393 (пользователь).
            cents = grouped_cents(value)
            message = f"«{columns[f].header}»: {value.shown} → {_euros(cents)}"
            parsed.issue(PRICE_FIXED, record, message)
            return cents
        return _read(lambda: values.parse_money(value, free_is_zero=True), columns[f])

    return read


def grouped_cents(value: Grouped) -> int:
    """Цена «197.393» → 19739 центов: после точки — не больше двух цифр, округление до цента."""
    return int((Decimal(int(value)) / 10).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _euros(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d}"


def _read[T](parse: Callable[[], T], column: Column | None) -> T:
    """Значение ячейки; ошибка разбора — с названием колонки."""
    try:
        return parse()
    except ValueError as error:
        name = f"«{column.header}»: " if column is not None else ""
        raise ValueError(f"{name}{error}") from None


def _service_or_none(text: str) -> PlacementType | None:
    try:
        return values.parse_service(text)
    except ValueError:
        return None


def _text(value: object) -> str | None:
    text = show(value) if value is not None else ""
    return text or None


def _empty(value: object) -> bool:
    return is_blank(value) or (isinstance(value, str) and value.strip().lower() in _NA)
