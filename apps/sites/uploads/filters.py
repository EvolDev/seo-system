"""Фильтры загрузки каталога: каких новых площадок добавлять (ADR-044).

Фильтр отсекает при «Добавить новые» то, что нам не нужно сейчас. Не
подошедшие площадки не записываются и не отклоняются: в следующей
выгрузке, когда у них вырастет DR, они пройдут.

Значения — как в выгрузке Collaborator (английский интерфейс): страны,
языки, dofollow, Yes/No. Фильтр по полю, где ничего не выбрано, не
ограничивает. У площадки нет значения, а диапазон задан — не проходит.

Одна и та же функция `matches` считает живой счётчик на экране и решает,
что записать: показали «будет добавлено 8 412» — столько и запишется.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from django.core.cache import cache

from apps.sites.models import Site, Upload
from apps.sites.uploads import catalog, values
from apps.sites.uploads.files import Table, is_blank, show

# Сколько живёт в кеше разобранный для фильтров файл: пересобрать — секунды.
ROWS_TTL = 7 * 24 * 3600
# Облачков-подсказок у поля из прошлых загрузок.
SUGGESTIONS = 8
# Сколько строк «что перезаписано» хранить в истории загрузки.
LIST_LIMIT = 300


@dataclass(frozen=True)
class FilterField:
    key: str
    header: str  # колонка выгрузки Collaborator
    label: str
    kind: Literal["any", "range"]
    unit: str = ""


FIELDS: tuple[FilterField, ...] = (
    FilterField("languages", catalog.LANGUAGES, "Языки", "any"),
    FilterField("country", "Country", "Страна", "any"),
    FilterField("dr", catalog.DR, "DR", "range"),
    FilterField("monthly_traffic", "Monthly Traffic", "Общий трафик (Monthly Traffic)", "range"),
    FilterField("organic_traffic", catalog.TRAFFIC, "Органический трафик", "range"),
    FilterField("keywords", catalog.KEYWORDS, "Ключи", "range"),
    FilterField("price", catalog.PRICE, "Цена публикации", "range", "€"),
    FilterField("link_type", catalog.LINK_TYPE, "Тип ссылки", "any"),
    FilterField("advertising", catalog.MARKS_AS_AD, "Пометка «реклама»", "any"),
)
BY_KEY = {field.key: field for field in FIELDS}
# Строка, которой нет в разборе для фильтров: не пройдёт ни один заданный фильтр.
EMPTY_ROW: tuple[Any, ...] = tuple(() if f.kind == "any" else None for f in FIELDS)
# Колонки, где в ячейке бывает несколько значений через запятую.
_LISTS = {"languages", "country"}

# Строка для фильтра: домен и значения полей в порядке FIELDS.
type FilterRow = tuple[str, tuple[Any, ...]]
type Spec = dict[str, Any]


def parse_spec(data: Any) -> Spec:
    """Фильтр из запроса: только известные поля, пустое отброшено.

    `{"country": ["USA", "India"], "dr": {"min": 30, "max": None}}`.
    """
    spec: Spec = {}
    if not isinstance(data, Mapping):
        return spec
    for field in FIELDS:
        raw = data.get(field.key)
        if field.kind == "any":
            chosen = _strings(raw)
            if chosen:
                spec[field.key] = chosen
        else:
            bounds = _bounds(raw)
            if bounds:
                spec[field.key] = bounds
    return spec


def matches(row: Sequence[Any], spec: Spec) -> bool:
    """Подходит ли строка фильтру. `row` — значения в порядке FIELDS."""
    for index, field in enumerate(FIELDS):
        rule = spec.get(field.key)
        if not rule:
            continue
        value = row[index]
        if field.kind == "any":
            have = {item.lower() for item in value}
            if not have & {item.lower() for item in rule}:
                return False
        else:
            if value is None:
                return False
            low, high = rule.get("min"), rule.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                return False
    return True


def describe(spec: Spec) -> str:
    """«DR ≥ 30 · страна: USA, India · тип ссылки: dofollow» — для истории загрузки."""
    parts: list[str] = []
    for field in FIELDS:
        rule = spec.get(field.key)
        if not rule:
            continue
        if field.kind == "any":
            parts.append(f"{field.label.lower()}: {', '.join(rule)}")
            continue
        low, high = rule.get("min"), rule.get("max")
        name = field.label if field.key == "dr" else field.label.lower()
        unit = field.unit
        if low is not None and high is not None:
            parts.append(f"{name} {unit}{_num(low)}–{unit}{_num(high)}")
        elif low is not None:
            parts.append(f"{name} ≥ {unit}{_num(low)}")
        else:
            parts.append(f"{name} ≤ {unit}{_num(high)}")
    return " · ".join(parts) or "без фильтра"


def rows_from_table(table: Table) -> list[FilterRow]:
    """Строки выгрузки → значения для фильтров. Дубли домена — первая строка, меньшая цена."""
    index = {column.header: column.index for column in table.columns}
    seen: dict[str, int] = {}
    rows: list[FilterRow] = []
    price_at = [f.key for f in FIELDS].index("price")
    for row in table.rows:
        try:
            domain, _ = values.parse_domain(row.get(index[catalog.DOMAIN]))
        except ValueError:
            continue
        # Колонки фильтра нет в выгрузке — значение пустое, а не ошибка.
        found = tuple(
            _value(field, row.get(index[field.header]) if field.header in index else None)
            for field in FIELDS
        )
        if domain in seen:
            position = seen[domain]
            old = rows[position][1]
            if found[price_at] is not None and (
                old[price_at] is None or found[price_at] < old[price_at]
            ):
                rows[position] = (domain, (*old[:price_at], found[price_at], *old[price_at + 1 :]))
            continue
        seen[domain] = len(rows)
        rows.append((domain, found))
    return rows


def cached_rows(upload: Upload, table: Table | None = None) -> list[FilterRow]:
    """Строки для фильтров из кеша; нет — разбор файла (секунды) и в кеш."""
    key = f"upload-filter-rows:{upload.pk}:{upload.file_sha256}"
    rows: list[FilterRow] | None = cache.get(key)
    if rows is None:
        if table is None:
            from apps.sites.uploads.service import table_for  # круговой импорт

            table = table_for(upload)
        rows = rows_from_table(table)
        cache.set(key, rows, ROWS_TTL)
    return rows


def known_domains(upload: Upload, rows: Sequence[FilterRow]) -> set[str]:
    """Какие домены файла уже есть в базе. В кеше до следующей записи этой загрузки."""
    version = len((upload.result or {}).get("runs", []))
    written = int(upload.written_at.timestamp() * 1_000_000) if upload.written_at else 0
    key = f"upload-known:{upload.pk}:{version}:{written}"
    known: set[str] | None = cache.get(key)
    if known is None:
        known = set()
        domains = [domain for domain, _ in rows]
        for start in range(0, len(domains), 5000):
            chunk = domains[start : start + 5000]
            # Удалённые — для загрузки новые: их снова можно добавить.
            known.update(
                Site.all_objects.filter(domain__in=chunk, is_deleted=False).values_list(
                    "domain", flat=True
                )
            )
        cache.set(key, known, ROWS_TTL)
    return known


@dataclass(frozen=True)
class Count:
    passed: int  # новых, подходят фильтру — столько добавится
    total: int  # новых для базы всего


def count_new(upload: Upload, spec: Spec) -> Count:
    rows = cached_rows(upload)
    known = known_domains(upload, rows)
    new = [row for domain, row in rows if domain not in known]
    return Count(sum(1 for row in new if matches(row, spec)), len(new))


def choices(upload: Upload) -> dict[str, list[tuple[str, int]]]:
    """Значения полей «любое из» у новых площадок файла, частые первыми — для поиска в поле."""
    rows = cached_rows(upload)
    known = known_domains(upload, rows)
    result: dict[str, list[tuple[str, int]]] = {}
    for index, field in enumerate(FIELDS):
        if field.kind != "any":
            continue
        counter: Counter[str] = Counter()
        for domain, row in rows:
            if domain not in known:
                counter.update(row[index])
        result[field.key] = counter.most_common()
    return result


def suggestions(uploads: Iterable[Upload]) -> dict[str, list[dict[str, Any]]]:
    """Облачка из прошлых загрузок: значения фильтров «Добавить новые», свежие первыми."""
    found: dict[str, list[dict[str, Any]]] = {field.key: [] for field in FIELDS}
    seen: dict[str, set[str]] = {field.key: set() for field in FIELDS}
    for upload in uploads:
        runs = (upload.result or {}).get("runs", [])
        for run in reversed(runs):
            spec = parse_spec(run.get("filters"))
            for field in FIELDS:
                rule = spec.get(field.key)
                if not rule:
                    continue
                chips: list[dict[str, Any]] = []
                if field.kind == "any":
                    chips = [{"value": value, "text": value} for value in rule]
                else:
                    for bound, sign in (("min", "от"), ("max", "до")):
                        if rule.get(bound) is not None:
                            number = rule[bound]
                            text = f"{sign} {field.unit}{_num(number)}"
                            chips.append({"bound": bound, "value": number, "text": text})
                for chip in chips:
                    marker = f"{chip.get('bound', '')}:{chip['value']}"
                    if marker in seen[field.key] or len(found[field.key]) >= SUGGESTIONS:
                        continue
                    seen[field.key].add(marker)
                    found[field.key].append(chip)
    return found


def _value(field: FilterField, raw: object) -> Any:
    if field.kind == "any":
        if is_blank(raw):
            return ()
        text = show(raw)
        if field.key in _LISTS:
            return tuple(part.strip() for part in text.split(",") if part.strip())
        return (text,)
    try:
        if field.key == "price":
            cents = values.parse_money(raw)
            return None if cents is None else cents / 100
        number = values.parse_number(raw)
    except ValueError:
        return None
    return number


def _strings(raw: Any) -> list[str]:
    if not isinstance(raw, list | tuple):
        return []
    result: list[str] = []
    for item in raw:
        text = " ".join(str(item).split())
        if text and text not in result:
            result.append(text)
    return result


def _bounds(raw: Any) -> dict[str, float | None]:
    if not isinstance(raw, Mapping):
        return {}
    bounds: dict[str, float | None] = {}
    for name in ("min", "max"):
        value = raw.get(name)
        if value is None or value == "":
            continue
        try:
            number = float(Decimal(str(value).replace(",", ".").replace(" ", "")))
        except ArithmeticError:
            continue
        bounds[name] = number
    if not bounds:
        return {}
    return {"min": bounds.get("min"), "max": bounds.get("max")}


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}" if value != int(value) else f"{int(value):,}".replace(",", " ")
