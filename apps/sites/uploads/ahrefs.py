"""Выгрузка Ahrefs Batch Analysis → наши замеры площадок базы (ADR-045).

Файл — UTF-16 с табуляцией, как его отдаёт кнопка Export (чтение — `files`),
разметка встроена. Страну выгрузки выбирает человек: в файле её нет. Но
выгрузку «все страны» видно по колонке «Organic / Top Countries» — при
выбранной стране её нет, по ней выбор и сверяется.

- **Все страны** → снимок `site_metrics`: DR, трафик, ключи, топ-регион и
  его трафик («(us, 21101)» — Top location и Location traffic на экране
  Ahrefs; Traffic — общий).
- **Страна** → снимок `site_country_metrics`: Organic / Traffic и Total
  Keywords — трафик и ключи этой страны.

Остальные колонки строки — целиком в `raw` замера. Пишется только к
площадкам базы, замер наш — без продавца, доверенный. Снимок дня — по
площадке (и стране), источнику и дате: повторная загрузка за тот же день
обновляет его, новая дата — новый снимок.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.sites import countries
from apps.sites.models import MetricSource, Site, SiteCountryMetric, SiteMetric
from apps.sites.uploads import values
from apps.sites.uploads.files import Table, is_blank, show
from apps.sites.uploads.plan import LIST_LIMIT

TARGET = "Target"
MODE = "Mode"
DR = "Domain Rating"
TRAFFIC = "Organic / Traffic"
KEYWORDS = "Organic / Total Keywords"
TOP_COUNTRIES = "Organic / Top Countries"
ROW_NUMBER = "#"  # номер строки выгрузки — в сырые данные не идёт

REQUIRED = (TARGET, MODE, DR, TRAFFIC, KEYWORDS)
# Метрики сайта целиком. «prefix» и «exact» — метрики одной страницы или раздела.
SITE_MODES = frozenset({"subdomains", "domain"})
SOURCE = MetricSource.AHREFS_BATCH
BATCH = 1000

# «(us, 21101)»; на случай нескольких стран берём первую — она топ.
_TOP = re.compile(r"\(\s*([a-z]{2})\s*,\s*([^)]*?)\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class Measure:
    """Строка выгрузки. Имена полей — как в моделях замеров."""

    line: int
    domain: str
    source_value: str | None  # Target, если там не просто домен
    dr: int | None
    organic_traffic: int | None
    total_keywords: int | None
    top_geo: str | None
    top_geo_traffic: int | None
    raw: dict[str, str]


@dataclass(frozen=True)
class Problem:
    line: int
    message: str


@dataclass
class Parsed:
    measures: list[Measure]
    errors: list[Problem] = field(default_factory=list)
    duplicates: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)


def looks_like_batch(cells: Sequence[str]) -> bool:
    return TARGET in cells and DR in cells


def missing_columns(table: Table) -> list[str]:
    headers = {column.header for column in table.columns}
    return [name for name in REQUIRED if name not in headers]


def is_all_countries(table: Table) -> bool:
    return any(column.header == TOP_COUNTRIES for column in table.columns)


def country_error(table: Table, country: str | None) -> str | None:
    """Выбор страны не сходится с файлом — текст для человека, иначе None."""
    if country and is_all_countries(table):
        return (
            f"Это выгрузка по всем странам — в ней есть колонка «{TOP_COUNTRIES}», "
            f"а выбрана страна {countries.name(country)}. Загрузите файл снова с "
            "«Все страны» или сделайте в Ahrefs выгрузку по этой стране."
        )
    if not country and not is_all_countries(table):
        return (
            f"Похоже, это выгрузка по одной стране: нет колонки «{TOP_COUNTRIES}». "
            "Загрузите файл снова и выберите страну, которая была выбрана в Ahrefs."
        )
    return None


def parse(table: Table) -> Parsed:
    """Строки файла → замеры. Битая строка — в ошибки, загрузку не останавливает."""
    index = {column.header: column.index for column in table.columns}
    parsed = Parsed(measures=[])
    first: dict[str, Measure] = {}
    lines: dict[str, list[int]] = {}
    for row in table.rows:
        cells = {header: row.get(position) for header, position in index.items()}
        try:
            measure = _measure(row.line, cells)
        except ValueError as error:
            parsed.errors.append(Problem(row.line, str(error)))
            continue
        lines.setdefault(measure.domain, []).append(row.line)
        if measure.domain not in first:
            first[measure.domain] = measure
    parsed.measures = list(first.values())
    parsed.duplicates = [(d, tuple(ls)) for d, ls in lines.items() if len(ls) > 1]
    return parsed


def _measure(line: int, cells: dict[str, object]) -> Measure:
    """Строка → замер; `cells` — заголовок → значение, нет колонки — нет ключа."""
    domain, is_url = values.parse_domain(cells.get(TARGET))
    mode = show(cells.get(MODE)).lower()
    if mode and mode not in SITE_MODES:
        raise ValueError(
            f"{domain}: режим «{mode}» — метрики страницы, а не сайта; "
            "в Ahrefs нужен режим Subdomains"
        )
    top_geo, top_traffic = _top_country(cells.get(TOP_COUNTRIES))
    raw = {
        header: show(value)
        for header, value in cells.items()
        if header != ROW_NUMBER and not is_blank(value)
    }
    return Measure(
        line=line,
        domain=domain,
        source_value=show(cells.get(TARGET)) if is_url else None,
        dr=_number(cells.get(DR), DR),
        organic_traffic=_number(cells.get(TRAFFIC), TRAFFIC),
        total_keywords=_number(cells.get(KEYWORDS), KEYWORDS),
        top_geo=top_geo,
        top_geo_traffic=top_traffic,
        raw=raw,
    )


def _number(value: object, header: str) -> int | None:
    # Пусто — «нет данных», а не ноль: у Ahrefs ноль пишется нулём.
    try:
        return values.parse_number(value)
    except ValueError as error:
        raise ValueError(f"«{header}»: {error}") from None


def _top_country(value: object) -> tuple[str | None, int | None]:
    text = show(value)
    if not text:
        return None, None
    match = _TOP.search(text)
    if match is None:
        raise ValueError(f"«{TOP_COUNTRIES}»: не разобрать «{text}»")
    return match.group(1).lower(), _number(match.group(2), TOP_COUNTRIES)


# --- План: что запишется ---


@dataclass
class Plan:
    parsed: Parsed
    country: str | None  # пусто — все страны
    checked_at: Any  # начало дня даты замера
    site_ids: dict[str, int]  # домен → площадка базы
    today: set[int]  # площадки, у которых снимок этого дня уже есть — обновится

    @property
    def known(self) -> list[Measure]:
        return [m for m in self.parsed.measures if m.domain in self.site_ids]

    def summary(self, *, rows: int, blank_rows: int) -> dict[str, Any]:
        measures = self.parsed.measures
        missing = [
            {"line": m.line, "domain": m.domain} for m in measures if m.domain not in self.site_ids
        ]
        errors = [{"line": e.line, "message": e.message} for e in self.parsed.errors]
        urls = [
            {"line": m.line, "value": m.source_value, "domain": m.domain}
            for m in measures
            if m.source_value
        ]
        duplicates = [{"domain": d, "lines": list(ls)} for d, ls in self.parsed.duplicates]
        known = self.known
        return {
            "rows": rows,
            "blank_rows": blank_rows,
            "country": self.country or "",
            "sites": len(measures),
            "known": len(known),
            "new": len(measures) - len(known),
            "today": len(self.today),
            "with_top_geo": sum(1 for m in known if m.top_geo),
            "without_traffic": sum(1 for m in known if m.organic_traffic is None),
            **_capped("missing", missing),
            **_capped("errors", errors),
            **_capped("urls", urls),
            **_capped("duplicates", duplicates),
        }


def build_plan(parsed: Parsed, *, country: str | None, checked_at: Any) -> Plan:
    domains = [m.domain for m in parsed.measures]
    site_ids: dict[str, int] = {}
    # Удалённая площадка для выгрузки — «не в базе»: замер к ней не пишется.
    for start in range(0, len(domains), BATCH):
        site_ids.update(
            Site.objects.filter(domain__in=domains[start : start + BATCH]).values_list(
                "domain", "pk"
            )
        )
    today = _day_rows(country, checked_at, list(site_ids.values()))
    return Plan(parsed, country, checked_at, site_ids, set(today))


# --- Запись ---

SITE_FIELDS = ["dr", "organic_traffic", "total_keywords", "top_geo", "top_geo_traffic", "raw"]
COUNTRY_FIELDS = ["organic_traffic", "total_keywords", "raw"]
type Snapshot = SiteMetric | SiteCountryMetric


def write(plan: Plan) -> dict[str, Any]:
    """Снимки дня: есть — обновляются, нет — создаются. Вызывается внутри транзакции записи."""
    measures = {plan.site_ids[m.domain]: m for m in plan.known}
    existing = _day_rows(plan.country, plan.checked_at, list(measures))
    model: type[Snapshot] = SiteCountryMetric if plan.country else SiteMetric
    names = COUNTRY_FIELDS if plan.country else SITE_FIELDS
    key = {"country": plan.country} if plan.country else {}
    new: list[Any] = []
    changed: list[Any] = []
    for site_id, measure in measures.items():
        data = {name: getattr(measure, name) for name in names}
        row = existing.get(site_id)
        if row is None:
            new.append(
                model(site_id=site_id, source=SOURCE, checked_at=plan.checked_at, **key, **data)
            )
        elif _apply(row, data):
            changed.append(row)
    model.objects.bulk_create(new, batch_size=BATCH)
    model.objects.bulk_update(changed, names, batch_size=BATCH)
    return {
        "counts": {
            "measures_created": len(new),
            "measures_updated": len(changed),
            "measures_unchanged": len(measures) - len(new) - len(changed),
        },
        "sites": len(measures),
    }


def _day_rows(country: str | None, checked_at: Any, ids: Sequence[int]) -> dict[int, Snapshot]:
    """Снимки этого дня из выгрузок Ahrefs у площадок `ids`: площадка → снимок."""
    rows: dict[int, Snapshot] = {}
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        found: Iterable[Snapshot]
        if country:
            found = SiteCountryMetric.objects.filter(
                site_id__in=chunk, country=country, source=SOURCE, checked_at=checked_at
            )
        else:
            # Только наш замер: снимок продавца того же дня — другой.
            found = SiteMetric.objects.filter(
                site_id__in=chunk, seller__isnull=True, source=SOURCE, checked_at=checked_at
            )
        rows.update((row.site_id, row) for row in found)
    return rows


def _apply(row: Snapshot, data: dict[str, Any]) -> bool:
    changed = False
    for name, value in data.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


def _capped(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {name: rows[:LIST_LIMIT], f"{name}_total": len(rows)}
