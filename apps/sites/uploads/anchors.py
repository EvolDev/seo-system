"""Файл анкоров продукта — экран «Загрузки» (E3-05, ADR-059).

Файл — как лист «Распределение анкоров» таблицы линкбилдинга: ключ, URL,
объёмы, позиции по датам («Pos 22.09.26»), Tool, Type. Колонки размечаются, как
у прайса: словарь синонимов подсказывает, человек подтверждает, разметка
запоминается по прошлым загрузкам анкоров. Placed и Waiting не читаются —
система считает их сама по ссылкам размещений.

В той же книге могут быть листы долей — их узнаём по заголовкам и разбираем
сами, без разметки: «Распределение по типам страниц», «Распределение
безанкорки», «Распределение по странам». Тот же разбор читает импорт таблицы
(E1-04).

Запись — сведение с тем, что есть: новые анкоры добавляются, у знакомых
факты из файла заменяют прежние (пустая ячейка ничего не стирает), позиции
ложатся снимками на дату колонки, доли — по строкам. Чего в файле нет, то в
базе остаётся. Повторная загрузка того же файла ничего не меняет.
"""

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from apps.keywords.models import (
    NAKED_TYPES,
    AnchorType,
    CountryShare,
    Keyword,
    KeywordPosition,
    PageTypeShare,
)
from apps.placements.models import PlacementLink
from apps.sites import countries
from apps.sites.models import MetricSource, Product, Upload, UploadKind, UploadStatus
from apps.sites.uploads.columns import Confidence
from apps.sites.uploads.files import (
    HEADER_SEARCH_ROWS,
    Column,
    SheetRows,
    Table,
    header_key,
    is_blank,
    read_sheets,
    show,
)

# Сколько строк каждой группы показать в сводке: остальное — числом.
SHOWN = 200
# Позиция «вне топ-100» в таблице — 101 (12-IMPORT-MAPPING.md §2.2).
OUT_OF_TOP = 101
# Позиции — по выдаче США, как в таблице.
POSITIONS_COUNTRY = "US"
PCT = Decimal("0.01")


class AField(StrEnum):
    """Куда записать колонку файла анкоров. Значение хранится в разметке загрузки."""

    KEYWORD = "keyword"
    URL = "url"
    VOLUME = "volume"
    GLOBAL_VOLUME = "global_volume"
    TOOL = "tool"
    PAGE_TYPE = "page_type"
    POSITION = "position"
    SKIP = "skip"

    @property
    def label(self) -> str:
        return FIELD_LABELS[self]


FIELD_LABELS: dict[AField, str] = {
    AField.KEYWORD: "Анкор (ключ)",
    AField.URL: "Куда ведёт — адрес страницы продукта",
    AField.VOLUME: "Объём (Volume)",
    AField.GLOBAL_VOLUME: "Глобальный объём (Global Volume)",
    AField.TOOL: "Раздел сайта (Tool)",
    AField.PAGE_TYPE: "Тип страницы (Type)",
    AField.POSITION: "Позиция на дату из заголовка («Pos 22.09.26»)",
    AField.SKIP: "Не загружать",
}
_VALUES = frozenset(f.value for f in AField)
# Одна колонка на поле; позиций и «не загружать» — сколько угодно.
SINGLE_FIELDS = frozenset(AField) - {AField.POSITION, AField.SKIP}

_EXACT: dict[str, AField] = {
    **dict.fromkeys(
        ("keyword", "keywords", "ключ", "ключи", "анкор", "anchor", "anchor text", "запрос"),
        AField.KEYWORD,
    ),
    **dict.fromkeys(
        ("url", "link", "ссылка", "адрес", "target url", "куда ведёт", "целевая страница"),
        AField.URL,
    ),
    **dict.fromkeys(("volume", "объём", "объем", "search volume", "частота"), AField.VOLUME),
    **dict.fromkeys(
        ("global volume", "глобальный объём", "глобальный объем"), AField.GLOBAL_VOLUME
    ),
    **dict.fromkeys(("tool", "раздел", "раздел сайта", "категория"), AField.TOOL),
    **dict.fromkeys(("type", "тип", "тип страницы", "page type"), AField.PAGE_TYPE),
}
# Placed и Waiting в таблице — формулы; система считает их сама.
_COUNTED = frozenset(
    ("links placed", "links waiting", "placed", "waiting", "размещено", "ждут", "в ожидании")
)
_POSITION = re.compile(r"^(?:pos|position|позиция|поз)\.?\s*(.*)$")
_DATE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})")


@dataclass(frozen=True)
class Guess:
    field: AField
    confidence: Confidence
    hint: str = ""


def position_date(header: str) -> dt.date | None:
    """Дата из заголовка позиции: «Pos 22.09.26» → 22.09.2026."""
    match = _DATE.search(header)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def guess_column(column: Column) -> Guess:
    key = _clean(column.key)
    exact = _EXACT.get(key)
    if exact is not None:
        return Guess(exact, Confidence.SURE)
    if key in _COUNTED:
        return Guess(
            AField.SKIP, Confidence.SURE, "Размещено и ждут система считает сама по размещениям."
        )
    if _POSITION.match(key):
        if position_date(key):
            return Guess(AField.POSITION, Confidence.SURE)
        return Guess(AField.SKIP, Confidence.LIKELY, "Позиция без даты в заголовке — не загружаю.")
    return Guess(AField.SKIP, Confidence.UNKNOWN)


def build_mapping(
    columns: Sequence[Column], remembered: Mapping[str, str] | None
) -> tuple[dict[str, Guess], list[str]]:
    """Разметка файла: запомненное по прошлым загрузкам + догадки. Второе — о чём спросить."""
    guesses = {column.key: guess_column(column) for column in columns}
    _one_column_per_field(columns, guesses)
    remembered = remembered or {}
    questions: list[str] = []
    for column in columns:
        known = remembered.get(column.key)
        if known in _VALUES:
            guesses[column.key] = Guess(AField(known), Confidence.REMEMBERED)
        elif not column.is_empty and guesses[column.key].confidence != Confidence.SURE:
            questions.append(column.key)
    return guesses, questions


def validate_mapping(mapping: Mapping[str, AField], columns: Sequence[Column]) -> list[str]:
    """Ошибки разметки человеческим языком; пусто — разметка годится."""
    errors: list[str] = []
    headers = {column.key: column.header for column in columns}
    by_field: dict[AField, list[str]] = {}
    for key, value in mapping.items():
        by_field.setdefault(value, []).append(headers.get(key, key))
    if AField.KEYWORD not in by_field:
        errors.append("Укажите колонку с анкором — ключом.")
    if AField.URL not in by_field:
        errors.append("Укажите колонку «куда ведёт» — адрес страницы продукта.")
    for value in sorted(SINGLE_FIELDS, key=list(AField).index):
        if len(by_field.get(value, [])) > 1:
            names = ", ".join(f"«{name}»" for name in by_field[value])
            errors.append(f"«{value.label}» выбрано у нескольких колонок: {names}.")
    for header in by_field.get(AField.POSITION, []):
        if position_date(header) is None:
            errors.append(f"У позиции «{header}» нет даты в заголовке — например, «Pos 22.09.26».")
    return errors


def remembered_mapping() -> dict[str, str]:
    """Разметка прошлых загрузок анкоров: заголовок → поле, свежая перекрывает старую."""
    remembered: dict[str, str] = {}
    confirmed = (UploadStatus.CHECKING, UploadStatus.CHECKED, UploadStatus.WRITING)
    uploads = (
        Upload.objects.filter(kind=UploadKind.ANCHORS, mapping__isnull=False)
        .filter(status__in=(*confirmed, UploadStatus.DONE))
        .order_by("created_at", "pk")
        .values_list("mapping", flat=True)
    )
    for mapping in uploads:
        if isinstance(mapping, dict):
            remembered.update({str(k): str(v) for k, v in mapping.items() if v in _VALUES})
    return remembered


def looks_like_anchors_header(cells: Sequence[str]) -> bool:
    """Строка заголовков листа анкоров: есть колонки анкора и адреса."""
    found = {_EXACT.get(_clean(header_key(cell))) for cell in cells}
    return AField.KEYWORD in found and AField.URL in found


# ---------- Разбор ----------


@dataclass(frozen=True)
class AnchorRow:
    line: int
    keyword: str
    url: str
    volume: int | None
    global_volume: int | None
    tool: str | None
    page_type: str | None
    positions: tuple[tuple[dt.date, int | None], ...]


@dataclass(frozen=True)
class TypeRow:
    page_type: str
    target: Decimal | None
    exact: Decimal | None
    diluted: Decimal | None
    naked: Decimal | None


@dataclass(frozen=True)
class NakedRow:
    text: str
    url: str
    share: Decimal | None


@dataclass(frozen=True)
class CountryRow:
    name: str
    country: str | None  # пусто — «остальные»
    share: Decimal


@dataclass
class Parsed:
    anchors: list[AnchorRow] = field(default_factory=list)
    types: list[TypeRow] = field(default_factory=list)
    naked: list[NakedRow] = field(default_factory=list)
    countries: list[CountryRow] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    rows: int = 0


def parse_anchors(table: Table, mapping: Mapping[str, AField], parsed: Parsed) -> None:
    """Строки листа анкоров по разметке. Дубль анкора — в замечания, берётся первая."""
    by_field: dict[AField, int] = {}
    positions: list[tuple[int, dt.date]] = []
    for column in table.columns:
        value = mapping.get(column.key)
        if value is None:
            continue
        if value == AField.POSITION:
            date = position_date(column.header)
            if date is not None:
                positions.append((column.index, date))
        elif value != AField.SKIP:
            by_field[value] = column.index
    seen: set[str] = set()
    for row in table.rows:
        parsed.rows += 1
        keyword = " ".join(show(row.get(by_field[AField.KEYWORD])).split())
        url = show(row.get(by_field[AField.URL])).strip()
        if not keyword:
            continue
        if not url.startswith(("http://", "https://")):
            parsed.issues.append(f"Строка {row.line}: у «{keyword}» нет адреса — не загружаю.")
            continue
        if keyword.casefold() in seen:
            parsed.issues.append(f"Строка {row.line}: «{keyword}» уже был выше — беру первый.")
            continue
        seen.add(keyword.casefold())
        parsed.anchors.append(
            AnchorRow(
                line=row.line,
                keyword=keyword,
                url=url,
                volume=_count(row, by_field.get(AField.VOLUME), "Volume", parsed),
                global_volume=_count(row, by_field.get(AField.GLOBAL_VOLUME), "Global", parsed),
                tool=_text(row.get(by_field[AField.TOOL])) if AField.TOOL in by_field else None,
                page_type=_text(row.get(by_field[AField.PAGE_TYPE]))
                if AField.PAGE_TYPE in by_field
                else None,
                positions=tuple(
                    (date, position)
                    for index, date in positions
                    if (position := _position(row.get(index))) is not _MISSING
                ),
            )
        )


def parse_share_sheets(sheets: Iterable[tuple[str, SheetRows]], parsed: Parsed) -> None:
    """Листы долей книги — по заголовкам, без разметки."""
    for _, rows in sheets:
        header_at, headers = _header(rows)
        if header_at is None:
            continue
        body = rows[header_at + 1 :]
        if "тип страницы" in headers and _find(headers, "целевая доля") is not None:
            _parse_types(body, headers, parsed)
        elif _find(headers, "anchor text") is not None and "%" in headers:
            _parse_naked(body, headers, parsed)
        elif "страна" in headers and _find(headers, "доля") is not None:
            _parse_countries(body, headers, parsed)


def read_shares(path: Path, parsed: Parsed) -> None:
    parse_share_sheets(read_sheets(path), parsed)


def _parse_types(rows: Sequence[Sequence[object]], headers: list[str], parsed: Parsed) -> None:
    name_at = headers.index("тип страницы")
    columns = {
        "target": _find(headers, "целевая доля"),
        "exact": _find(headers, "прямой"),
        "diluted": _find(headers, "разбавлен"),
        "naked": _find(headers, "безанкор"),
    }
    raw: list[tuple[str, dict[str, Decimal | None]]] = []
    for cells in rows:
        name = _text(_cell(cells, name_at))
        if not name:
            continue
        raw.append((name, {k: _decimal(_cell(cells, i)) for k, i in columns.items()}))
    fraction = _fractions(v for _, values in raw for v in values.values())
    for name, values in raw:
        parsed.types.append(
            TypeRow(
                page_type=name,
                **{k: _pct(v, fraction) for k, v in values.items()},
            )
        )


def _parse_naked(rows: Sequence[Sequence[object]], headers: list[str], parsed: Parsed) -> None:
    text_at = _find(headers, "anchor text")
    url_at = _find(headers, "url")
    share_at = headers.index("%")
    raw: list[tuple[str, str, Decimal | None]] = []
    for cells in rows:
        text = " ".join(show(_cell(cells, text_at)).split())
        url = show(_cell(cells, url_at)).strip()
        if text and url.startswith(("http://", "https://")):
            raw.append((text, url, _decimal(_cell(cells, share_at))))
    fraction = _fractions(share for _, _, share in raw)
    parsed.naked.extend(NakedRow(text, url, _pct(share, fraction)) for text, url, share in raw)


def _parse_countries(rows: Sequence[Sequence[object]], headers: list[str], parsed: Parsed) -> None:
    name_at = headers.index("страна")
    share_at = _find(headers, "доля")
    raw: list[tuple[str, Decimal | None]] = []
    for cells in rows:
        name = _text(_cell(cells, name_at))
        if name:
            raw.append((name, _decimal(_cell(cells, share_at))))
    fraction = _fractions(share for _, share in raw)
    for name, share in raw:
        value = _pct(share, fraction)
        if value is None:
            continue
        code = country_code(name)
        if code == "":
            parsed.issues.append(f"Страна «{name}» не узнана — её долю не загружаю.")
            continue
        parsed.countries.append(CountryRow(name, code, value))


_OTHERS = frozenset(("остальные", "другие", "прочие", "others", "other", "rest"))


def country_code(name: str) -> str | None:
    """«США» → us, «Остальные» → None, не узнали — пустая строка."""
    cleaned = name.strip().casefold()
    if cleaned in _OTHERS:
        return None
    if countries.is_known(cleaned):
        return cleaned
    every = countries.all_countries()
    # Сначала название целиком: «США» есть и в поиске «Виргинских островов США».
    for country in every:
        if cleaned == country.name.casefold():
            return country.code
    for country in every:
        if cleaned in country.search.split():
            return country.code
    return ""


# ---------- План ----------


@dataclass
class Change:
    keyword: Keyword
    values: dict[str, Any]
    shown: list[tuple[str, str, str]]


@dataclass
class AnchorPlan:
    """Что сделает запись: по свежему состоянию базы, сводка и запись строят его одинаково."""

    product: Product
    parsed: Parsed
    new: list[AnchorRow] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    quiet: list[Change] = field(default_factory=list)
    same: int = 0
    missing: list[str] = field(default_factory=list)
    positions_new: int = 0
    positions_changed: int = 0
    positions_same: int = 0
    naked_new: list[NakedRow] = field(default_factory=list)
    naked_changed: list[tuple[Keyword, NakedRow]] = field(default_factory=list)
    naked_same: int = 0
    types_new: list[TypeRow] = field(default_factory=list)
    types_changed: list[tuple[PageTypeShare, TypeRow]] = field(default_factory=list)
    types_same: int = 0
    countries_new: list[CountryRow] = field(default_factory=list)
    countries_changed: list[tuple[CountryShare, CountryRow]] = field(default_factory=list)
    countries_same: int = 0

    def summary(self) -> dict[str, Any]:
        parsed = self.parsed
        dates = sorted({date for row in parsed.anchors for date, _ in row.positions})
        return {
            "rows": parsed.rows,
            "anchors": len(parsed.anchors),
            "new": [
                {"keyword": r.keyword, "url": r.url, "page_type": r.page_type or ""}
                for r in self.new[:SHOWN]
            ],
            "new_total": len(self.new),
            "changed": [
                {"keyword": c.keyword.keyword, "changes": c.shown} for c in self.changed[:SHOWN]
            ],
            "changed_total": len(self.changed),
            "same": self.same,
            "missing": self.missing[:SHOWN],
            "missing_total": len(self.missing),
            "dates": [f"{date:%d.%m.%Y}" for date in dates],
            "positions_new": self.positions_new,
            "positions_changed": self.positions_changed,
            "positions_same": self.positions_same,
            "naked": [
                {"text": r.text, "share": _pct_text(r.share), "was": ""} for r in self.naked_new
            ]
            + [
                {"text": r.text, "share": _pct_text(r.share), "was": _pct_text(k.share)}
                for k, r in self.naked_changed
            ],
            "naked_new": len(self.naked_new),
            "naked_changed": len(self.naked_changed),
            "naked_same": self.naked_same,
            "types": [_type_json(r, None) for r in self.types_new]
            + [_type_json(r, s) for s, r in self.types_changed],
            "types_new": len(self.types_new),
            "types_changed": len(self.types_changed),
            "types_same": self.types_same,
            "countries": [
                {"name": r.name, "share": _pct_text(r.share), "was": ""} for r in self.countries_new
            ]
            + [
                {"name": r.name, "share": _pct_text(r.share), "was": _pct_text(s.target_pct)}
                for s, r in self.countries_changed
            ],
            "countries_new": len(self.countries_new),
            "countries_changed": len(self.countries_changed),
            "countries_same": self.countries_same,
            "issues": parsed.issues[:SHOWN],
            "issues_total": len(parsed.issues),
        }

    @property
    def nothing(self) -> bool:
        return not (
            self.new
            or self.changed
            or self.positions_new
            or self.positions_changed
            or self.naked_new
            or self.naked_changed
            or self.types_new
            or self.types_changed
            or self.countries_new
            or self.countries_changed
        )


_FACTS = (
    ("target_url", "url", "Куда ведёт"),
    ("volume", "volume", "Объём"),
    ("global_volume", "global_volume", "Глобальный объём"),
    ("tool", "tool", "Раздел"),
    ("page_type", "page_type", "Тип страницы"),
)


def build_plan(parsed: Parsed, product: Product) -> AnchorPlan:
    plan = AnchorPlan(product=product, parsed=parsed)
    existing = _by_text(product)
    in_file: set[str] = set()
    known_rows: list[tuple[Keyword, AnchorRow]] = []
    for row in parsed.anchors:
        in_file.add(row.keyword.casefold())
        keyword = existing.get(row.keyword.casefold())
        if keyword is None:
            plan.new.append(row)
            continue
        known_rows.append((keyword, row))
        values: dict[str, Any] = {}
        shown: list[tuple[str, str, str]] = []
        for attr, source, label in _FACTS:
            new = getattr(row, source)
            old = getattr(keyword, attr)
            # Пустая ячейка ничего не стирает.
            if new is None or new == "" or new == old:
                continue
            values[attr] = new
            shown.append((label, _shown(old), _shown(new)))
        if keyword.anchor_type is None:
            values["anchor_type"] = AnchorType.EXACT
        if not keyword.is_active:
            values["is_active"] = True
            shown.append(("Активен", "нет", "да"))
        if shown:
            plan.changed.append(Change(keyword, values, shown))
        else:
            plan.same += 1
            if values:
                # Только тип «прямой» у ключа без типа — человеку это не новость.
                plan.quiet.append(Change(keyword, values, []))
    plan.missing = sorted(
        k.keyword
        for key, k in existing.items()
        if key not in in_file and k.is_active and k.anchor_type not in NAKED_TYPES
    )
    _plan_positions(plan, known_rows)
    _plan_naked(plan, existing)
    _plan_types(plan)
    _plan_countries(plan)
    return plan


def _plan_positions(plan: AnchorPlan, known: list[tuple[Keyword, AnchorRow]]) -> None:
    ids = [k.pk for k, _ in known]
    current = {
        (keyword_id, checked_at): position
        for keyword_id, checked_at, position in KeywordPosition.objects.filter(
            keyword_id__in=ids, country=POSITIONS_COUNTRY
        ).values_list("keyword_id", "checked_at", "position")
    }
    for keyword, row in known:
        for date, position in row.positions:
            key = (keyword.pk, date)
            if key not in current:
                plan.positions_new += 1
            elif current[key] != position:
                plan.positions_changed += 1
            else:
                plan.positions_same += 1
    for row in plan.new:
        plan.positions_new += len(row.positions)


def _plan_naked(plan: AnchorPlan, existing: dict[str, Keyword]) -> None:
    for row in plan.parsed.naked:
        keyword = existing.get(row.text.casefold())
        if keyword is None:
            plan.naked_new.append(row)
        elif keyword.share != row.share or keyword.anchor_type not in NAKED_TYPES:
            plan.naked_changed.append((keyword, row))
        else:
            plan.naked_same += 1


def _plan_types(plan: AnchorPlan) -> None:
    current = {s.page_type: s for s in PageTypeShare.objects.filter(product=plan.product)}
    for row in plan.parsed.types:
        share = current.get(row.page_type)
        if share is None:
            plan.types_new.append(row)
        elif (share.target_pct, share.exact_pct, share.diluted_pct, share.naked_pct) != (
            row.target,
            row.exact,
            row.diluted,
            row.naked,
        ):
            plan.types_changed.append((share, row))
        else:
            plan.types_same += 1


def _plan_countries(plan: AnchorPlan) -> None:
    current = {s.country: s for s in CountryShare.objects.filter(product=plan.product)}
    for row in plan.parsed.countries:
        share = current.get(row.country)
        if share is None:
            plan.countries_new.append(row)
        elif share.target_pct != row.share:
            plan.countries_changed.append((share, row))
        else:
            plan.countries_same += 1


# ---------- Запись ----------


def write(plan: AnchorPlan) -> dict[str, int]:
    """Сведение плана с базой — одной транзакцией. Итог — счётчики для экрана."""
    product = plan.product
    counts = {
        "created": 0,
        "updated": 0,
        "positions": 0,
        "naked": 0,
        "types": 0,
        "countries": 0,
        "links": 0,
    }
    with transaction.atomic():
        created: dict[str, Keyword] = {}
        for row in plan.new:
            created[row.keyword.casefold()] = Keyword.objects.create(
                product=product,
                keyword=row.keyword,
                target_url=row.url,
                volume=row.volume,
                global_volume=row.global_volume,
                tool=row.tool or None,
                page_type=row.page_type or None,
                anchor_type=AnchorType.EXACT,
            )
            counts["created"] += 1
        for change in [*plan.changed, *plan.quiet]:
            for attr, value in change.values.items():
                setattr(change.keyword, attr, value)
            change.keyword.save(update_fields=list(change.values))
            counts["updated"] += 1 if change.shown else 0
        counts["positions"] = _write_positions(plan, created)
        counts["naked"] = _write_naked(plan)
        counts["types"] = _write_types(plan)
        counts["countries"] = _write_countries(plan)
        counts["links"] = attach_links(product)
    return counts


def _write_positions(plan: AnchorPlan, created: dict[str, Keyword]) -> int:
    existing = _by_text(plan.product)
    written = 0
    for row in plan.parsed.anchors:
        keyword = created.get(row.keyword.casefold()) or existing.get(row.keyword.casefold())
        if keyword is None:
            continue
        for date, position in row.positions:
            _, made = KeywordPosition.objects.update_or_create(
                keyword=keyword,
                country=POSITIONS_COUNTRY,
                checked_at=date,
                defaults={"position": position, "source": MetricSource.CSV_IMPORT},
            )
            written += 1 if made else 0
    return written + plan.positions_changed


def _write_naked(plan: AnchorPlan) -> int:
    """Безанкорные анкоры: тип по тексту, тип страницы — как у ключа с тем же адресом."""
    page_types = _page_types_by_url(plan.product)
    written = 0
    for row in plan.naked_new:
        Keyword.objects.create(
            product=plan.product,
            keyword=row.text,
            target_url=row.url,
            anchor_type=naked_type(row.text, plan.product),
            page_type=page_types.get(_url_key(row.url)),
            share=row.share,
        )
        written += 1
    for keyword, row in plan.naked_changed:
        keyword.share = row.share
        fields = ["share"]
        if keyword.anchor_type not in NAKED_TYPES:
            keyword.anchor_type = naked_type(row.text, plan.product)
            fields.append("anchor_type")
        keyword.save(update_fields=fields)
        written += 1
    return written


def _write_types(plan: AnchorPlan) -> int:
    order = {row.page_type: number for number, row in enumerate(plan.parsed.types, start=1)}
    now = timezone.now()
    written = 0
    for row in plan.types_new:
        PageTypeShare.objects.create(
            product=plan.product,
            page_type=row.page_type,
            target_pct=row.target,
            exact_pct=row.exact,
            diluted_pct=row.diluted,
            naked_pct=row.naked,
            position=order[row.page_type],
        )
        written += 1
    for share, row in plan.types_changed:
        share.target_pct, share.exact_pct = row.target, row.exact
        share.diluted_pct, share.naked_pct = row.diluted, row.naked
        share.updated_at = now
        share.save()
        written += 1
    return written


def _write_countries(plan: AnchorPlan) -> int:
    now = timezone.now()
    for row in plan.countries_new:
        CountryShare.objects.create(product=plan.product, country=row.country, target_pct=row.share)
    for share, row in plan.countries_changed:
        share.target_pct = row.share
        share.updated_at = now
        share.save(update_fields=["target_pct", "updated_at"])
    return len(plan.countries_new) + len(plan.countries_changed)


def attach_links(product: Product) -> int:
    """Ссылки без анкора из списка — к анкору с тем же текстом (без регистра).

    Старые «Convertio», «click here» из таблицы получают анкор, когда в список
    пришла безанкорка: так они считаются в долях.
    """
    anchors = {
        text: (pk, kind)
        for text, pk, kind in Keyword.objects.filter(product=product, is_active=True)
        .annotate(text=Lower("keyword"))
        .values_list("text", "pk", "anchor_type")
    }
    attached = 0
    links = PlacementLink.objects.filter(placement__product=product, keyword__isnull=True)
    for link in links.annotate(text=Lower("anchor")).only("pk", "anchor"):
        found = anchors.get(link.text)
        if found is None:
            continue
        link.keyword_id, kind = found
        link.anchor_type = kind or AnchorType.EXACT
        link.save(update_fields=["keyword", "anchor_type"])
        attached += 1
    return attached


def naked_type(text: str, product: Product) -> AnchorType:
    """Безанкорный по тексту: адрес, бренд (есть название продукта) или нейтральное слово."""
    cleaned = text.strip().casefold()
    domain = product.domain.casefold()
    if cleaned.startswith(("http://", "https://", "www.")) or cleaned.rstrip("/") == domain:
        return AnchorType.URL
    if product.name.casefold() in cleaned or domain in cleaned:
        return AnchorType.BRANDED
    return AnchorType.GENERIC


# ---------- Мелочи ----------


def _by_text(product: Product) -> dict[str, Keyword]:
    return {k.keyword.casefold(): k for k in Keyword.objects.filter(product=product)}


def _page_types_by_url(product: Product) -> dict[str, str]:
    found: dict[str, str] = {}
    for url, page_type in (
        Keyword.objects.filter(product=product, page_type__isnull=False)
        .exclude(anchor_type__in=NAKED_TYPES)
        .order_by("pk")
        .values_list("target_url", "page_type")
    ):
        if page_type:
            found.setdefault(_url_key(url), page_type)
    return found


def _url_key(url: str) -> str:
    return url.strip().casefold().rstrip("/")


def _header(rows: SheetRows) -> tuple[int | None, list[str]]:
    for number, cells in enumerate(rows[:HEADER_SEARCH_ROWS]):
        texts = [header_key(show(cell)) for cell in cells]
        if sum(1 for text in texts if text) >= 2:
            return number, texts
    return None, []


def _find(headers: list[str], part: str) -> int | None:
    for index, header in enumerate(headers):
        if part in header:
            return index
    return None


def _cell(cells: Sequence[object], index: int | None) -> object:
    if index is None or index >= len(cells):
        return None
    return cells[index]


def _text(value: object) -> str | None:
    text = " ".join(show(value).split())
    return text or None


def _decimal(value: object) -> Decimal | None:
    if is_blank(value) or isinstance(value, bool):
        return None
    text = show(value).replace("%", "").replace(",", ".").replace("\xa0", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _fractions(values: Iterable[Decimal | None]) -> bool:
    """Доли записаны дробями (0.15), а не процентами (15): все не больше единицы."""
    present = [v for v in values if v is not None]
    return bool(present) and all(v <= 1 for v in present)


def _pct(value: Decimal | None, fraction: bool) -> Decimal | None:
    if value is None:
        return None
    return ((value * 100) if fraction else value).quantize(PCT)


def _count(row: Any, index: int | None, name: str, parsed: Parsed) -> int | None:
    if index is None:
        return None
    value = row.get(index)
    if is_blank(value):
        return None
    number = _decimal(str(value).replace(" ", "").replace(",", ""))
    if number is None or number < 0:
        parsed.issues.append(f"Строка {row.line}: {name} «{show(value)}» не число — пропускаю.")
        return None
    return int(number)


class _Missing:
    pass


_MISSING: Any = _Missing()


def _position(value: object) -> Any:
    """Позиция ячейки: число, 101 — «вне топ-100» (пусто), пустая ячейка — снимка нет."""
    number = _decimal(value)
    if number is None or number <= 0:
        return _MISSING
    position = int(number)
    return None if position >= OUT_OF_TOP else position


def _clean(key: str) -> str:
    return " ".join(key.replace("\xa0", " ").split()).strip(" ,.:;-_()").casefold()


def _one_column_per_field(columns: Sequence[Column], guesses: dict[str, Guess]) -> None:
    taken: set[AField] = set()
    for column in columns:
        guess = guesses[column.key]
        if guess.field in SINGLE_FIELDS:
            if guess.field in taken:
                guesses[column.key] = Guess(AField.SKIP, Confidence.UNKNOWN)
            taken.add(guess.field)


def _shown(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _pct_text(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value.normalize():f}".replace(".", ",") + "%"


def _type_json(row: TypeRow, share: PageTypeShare | None) -> dict[str, str]:
    def pair(new: Decimal | None, old: Decimal | None) -> str:
        if share is None or new == old:
            return _pct_text(new)
        return f"{_pct_text(old)} → {_pct_text(new)}"

    return {
        "page_type": row.page_type,
        "target": pair(row.target, share.target_pct if share else None),
        "exact": pair(row.exact, share.exact_pct if share else None),
        "diluted": pair(row.diluted, share.diluted_pct if share else None),
        "naked": pair(row.naked, share.naked_pct if share else None),
        "state": "новый" if share is None else "изменится",
    }
