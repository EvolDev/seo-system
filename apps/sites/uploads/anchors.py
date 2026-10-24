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
ложатся снимками на дату колонки, доли — по строкам. Полный список отключает
отсутствующие анкоры загружаемого вида; дополнение сохраняет их активными.
Вернувшиеся включаются с прежним ID, в том числе при повторе того же файла.

Файл — анкоры одного региона (ADR-075): сводится со списком анкоров продукта
этого региона, нет списка — запись его заводит. Позиции — по выдаче страны
региона. В книге с листами анкоров нескольких регионов («…EN», «…BR») берётся
лист с пометкой региона в названии. Цели долей и страны — общие для продукта;
лист безанкорки без пометки региона — основного списка (США).
"""

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone, translation
from django_countries import countries as country_names

from apps.keywords import matching
from apps.keywords.models import (
    DEFAULT_REGION,
    NAKED_TYPES,
    AnchorList,
    AnchorType,
    CountryShare,
    Keyword,
    KeywordPosition,
    PageTypeShare,
)
from apps.placements.models import PlacementLink
from apps.sites import countries
from apps.sites.languages import country_language
from apps.sites.models import MetricSource, Product, Upload, UploadKind, UploadStatus
from apps.sites.uploads.columns import Confidence
from apps.sites.uploads.files import (
    HEADER_SEARCH_ROWS,
    Column,
    FileError,
    SheetRows,
    Table,
    header_key,
    is_blank,
    read_sheets,
    recognized,
    show,
)

# Сколько строк каждой группы показать в сводке: остальное — числом.
SHOWN = 200
# Позиция «вне топ-100» в таблице — 101 (12-IMPORT-MAPPING.md §2.2).
OUT_OF_TOP = 101
PCT = Decimal("0.01")

# Параметр разбора хранится рядом с разметкой; это не колонка файла.
MODE_KEY = "__anchor_mode"
POLICY_KEY = "__anchor_policy"


class Policy(StrEnum):
    MERGE = "merge"
    SYNC = "sync"


POLICY_CHOICES = [
    (Policy.MERGE.value, "Дополнить список — отсутствующие остаются активными"),
    (Policy.SYNC.value, "Полный список — отключить отсутствующие анкоры"),
]


def policy(mapping: Mapping[str, str] | None) -> Policy:
    return Policy((mapping or {}).get(POLICY_KEY, Policy.MERGE))


class Mode(StrEnum):
    AUTO = "auto"
    NAKED = "naked"


MODE_CHOICES = [
    (Mode.AUTO.value, "Определить по файлу"),
    (Mode.NAKED.value, "Все строки — безанкорные ссылки"),
]


def mode(mapping: Mapping[str, str] | None) -> Mode:
    return Mode((mapping or {}).get(MODE_KEY, Mode.AUTO))


def column_mapping(mapping: Mapping[str, str]) -> dict[str, "AField"]:
    return {
        key: AField(value) for key, value in mapping.items() if key not in (MODE_KEY, POLICY_KEY)
    }


class AField(StrEnum):
    """Куда записать колонку файла анкоров. Значение хранится в разметке загрузки."""

    KEYWORD = "keyword"
    URL = "url"
    VOLUME = "volume"
    GLOBAL_VOLUME = "global_volume"
    TOOL = "tool"
    PAGE_TYPE = "page_type"
    SHARE = "share"
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
    AField.SHARE: "Доля внутри безанкорки, %",
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
    **dict.fromkeys(("%", "share", "share %", "доля", "доля, %", "доля %"), AField.SHARE),
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


# ---------- Регион файла (ADR-075) ----------

# Как ещё пишут регион в названии листа или файла — кроме кода страны и языка выдачи.
_REGION_WORDS: dict[str, frozenset[str]] = {
    "us": frozenset({"usa"}),
    "gb": frozenset({"uk"}),
}


@dataclass(frozen=True)
class Region:
    """Регион загрузки: страна, основной ли это список продукта, регионы других списков."""

    country: str
    is_main: bool
    others: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return countries.name(self.country)

    @property
    def positions_country(self) -> str:
        """Страна позиций — как в `keyword_positions`: заглавными (US, BR)."""
        return self.country.upper()


def region_of(product: Product, country: str | None) -> Region:
    """Регион загрузки у продукта. Не указан (загрузки до регионов) — основной список."""
    lists = AnchorList.of_product(product.pk)
    main = lists[0] if lists else None
    code = (country or (main.country if main is not None else DEFAULT_REGION)).lower()
    return Region(
        country=code,
        is_main=main is None or main.country == code,
        others=tuple(item.country for item in lists if item.country != code),
    )


def marked_for(title: str, country: str) -> bool:
    """В названии листа или файла пометка региона: код страны, язык выдачи, название.

    «Распределение анкоров BR» — Бразилия, «…EN» — США (выдача на английском).
    """
    words = tuple(re.findall(r"\w+", title.casefold()))
    marks = {country, *_REGION_WORDS.get(country, ())}
    language = country_language(country)
    if language is not None:
        marks.add(language)
    if marks.intersection(words):
        return True
    return any(_has_words(words, name) for name in _country_names(country))


@cache
def _country_names(country: str) -> tuple[tuple[str, ...], ...]:
    """Название страны по-русски и по-английски — словами."""
    with translation.override("en"):
        english = str(country_names.name(country.upper()))
    names = (countries.name(country), english)
    return tuple(found for name in names if (found := tuple(re.findall(r"\w+", name.casefold()))))


def _has_words(words: Sequence[str], name: Sequence[str]) -> bool:
    size = len(name)
    return any(tuple(words[i : i + size]) == tuple(name) for i in range(len(words) - size + 1))


def anchor_sheet(sheets: Sequence[tuple[str, SheetRows]], region: Region) -> str | None:
    """Лист анкоров книги для региона: один — он, несколько — с пометкой региона.

    Не нашли, какой брать, или лист помечен другим регионом продукта — FileError:
    анкоры одного региона в список другого не пишем.
    """
    found = [title for title, rows in sheets if recognized(looks_like_anchors_header)(rows)]
    # Лист безанкорки («Anchor text», «URL», «%») тоже узнаётся как анкоры — он
    # читается листом долей, а не основным листом.
    naked = {title for title, rows in sheets if _is_naked_sheet(rows)}
    found = [title for title in found if title not in naked] or found
    if not found:
        return None
    if len(found) == 1:
        check_marks(found[0], region, what="лист")
        return found[0]
    mine = [title for title in found if marked_for(title, region.country)]
    if len(mine) == 1:
        return mine[0]
    names = ", ".join(f"«{title}»" for title in found)
    raise FileError(
        f"В книге несколько листов анкоров: {names}. Для региона «{region.name}» не понял,"
        f" какой брать: в названии нужного листа должно быть «{region.country.upper()}»"
        f" или «{region.name}». Переименуйте лист или загрузите его отдельным файлом."
    )


def _is_naked_sheet(rows: SheetRows) -> bool:
    _, headers = _header(rows)
    return _find(headers, "anchor text") is not None and "%" in headers


def check_marks(title: str, region: Region, *, what: str) -> None:
    """Лист или файл помечен другим регионом, а своим — нет: FileError.

    Другой регион — регион другого списка продукта (в том числе по языку:
    «EN» — США) или явная пометка любой страны: код заглавными («BR») или
    название («Бразилия»). Так файл «…анкоров BR» не уйдёт в список США, даже
    если списка Бразилии ещё нет и регион забыли выбрать.
    """
    if marked_for(title, region.country):
        return
    found = [other for other in region.others if marked_for(title, other)]
    found += [code for code in explicit_countries(title) if code != region.country]
    if found:
        name = countries.name(found[0])
        raise FileError(
            f"Похоже, это анкоры региона «{name}»: так помечен {what} «{title}»."
            f" Выберите регион «{name}» или переименуйте {what}."
        )


def explicit_countries(title: str) -> list[str]:
    """Страны, явно названные в названии листа или файла: код заглавными или название.

    Код — только заглавными: «BR», а не «co» из «convertio.co»; язык («EN», «PT»)
    страной не считается — его понимает `marked_for` своего региона.
    """
    codes = [
        word.lower()
        for word in re.findall(r"\w+", title)
        if len(word) == 2 and word.isupper() and countries.is_known(word.lower())
    ]
    words = tuple(re.findall(r"\w+", title.casefold()))
    # Короткие названия одним словом («Чад», «США») словами заголовка не считаем:
    # слишком легко совпасть случайно. США свой регион узнаёт и так (`marked_for`).
    named = [
        country.code
        for country in countries.all_countries()
        if any(
            _has_words(words, name)
            for name in _country_names(country.code)
            if len(name) > 1 or len(name[0]) > 3
        )
    ]
    return list(dict.fromkeys(codes + named))


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
    # Что из книги не взято и почему — не ошибка файла, полный список не блокирует.
    notes: list[str] = field(default_factory=list)
    sheet: str | None = None  # лист анкоров книги
    rows: int = 0
    naked_only: bool = False


def parse_anchors(
    table: Table, mapping: Mapping[str, AField], parsed: Parsed, *, as_naked: bool = False
) -> None:
    """Строки листа анкоров по разметке. Дубль анкора — в замечания, берётся первая."""
    parsed.naked_only = as_naked
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
    naked: list[tuple[str, str, Decimal | None]] = []
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
        if as_naked:
            share_at = by_field.get(AField.SHARE)
            naked.append(
                (keyword, url, _decimal(row.get(share_at)) if share_at is not None else None)
            )
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
    if as_naked:
        fraction = _fractions(share for _, _, share in naked)
        parsed.naked.extend(
            NakedRow(text, url, _pct(share, fraction)) for text, url, share in naked
        )


def parse_share_sheets(
    sheets: Iterable[tuple[str, SheetRows]], parsed: Parsed, region: Region | None = None
) -> None:
    """Листы долей книги — по заголовкам, без разметки.

    Цели по типам страниц и странам — общие для продукта, берутся всегда.
    Безанкорка — анкоры списка: с `region` — только лист этого региона (`_naked_applies`).
    """
    for title, rows in sheets:
        header_at, headers = _header(rows)
        if header_at is None:
            continue
        body = rows[header_at + 1 :]
        if "тип страницы" in headers and _find(headers, "целевая доля") is not None:
            _parse_types(body, headers, parsed)
        elif _find(headers, "anchor text") is not None and "%" in headers:
            if region is None or _naked_applies(title, region, parsed):
                _parse_naked(body, headers, parsed)
        elif "страна" in headers and _find(headers, "доля") is not None:
            _parse_countries(body, headers, parsed)


def _naked_applies(title: str, region: Region, parsed: Parsed) -> bool:
    """Лист безанкорки — этого региона: помечен им, а без пометки — основного списка."""
    if marked_for(title, region.country):
        return True
    for other in region.others:
        if marked_for(title, other):
            parsed.notes.append(
                f"Лист «{title}» — безанкорка региона «{countries.name(other)}»:"
                f" к списку «{region.name}» не применяю."
            )
            return False
    if region.is_main:
        return True
    parsed.notes.append(
        f"Лист «{title}» без региона в названии — это безанкорка основного списка:"
        f" к списку «{region.name}» не применяю. Безанкорку региона — листом с"
        f" «{region.country.upper()}» в названии или отдельным файлом."
    )
    return False


def read_shares(path: Path, parsed: Parsed, region: Region | None = None) -> None:
    parse_share_sheets(read_sheets(path), parsed, region)


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
    seen = {row.text.casefold() for row in parsed.naked}
    for cells in rows:
        text = " ".join(show(_cell(cells, text_at)).split())
        url = show(_cell(cells, url_at)).strip()
        if text and url.startswith(("http://", "https://")):
            if text.casefold() in seen:
                # Явная разметка уже прочитала этот лист; дубль не создаёт запись.
                continue
            seen.add(text.casefold())
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
    """Что сделает запись: по свежему состоянию базы, сводка и запись строят его одинаково.

    `anchor_list` — список региона; пусто — его заведёт запись.
    """

    product: Product
    parsed: Parsed
    region: Region
    anchor_list: AnchorList | None = None
    new: list[AnchorRow] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    quiet: list[Change] = field(default_factory=list)
    same: int = 0
    missing: list[str] = field(default_factory=list)
    deactivate: list[Keyword] = field(default_factory=list)
    reactivated: set[int] = field(default_factory=set)
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
        naked = {r.text.casefold(): naked_type(r.text, self.product).label for r in parsed.naked}
        dates = sorted({date for row in parsed.anchors for date, _ in row.positions})
        return {
            "region": self.region.name,
            "region_flag": countries.flag_code(self.region.country) or "",
            "positions_country": self.region.positions_country,
            "new_list": self.anchor_list is None,
            "sheet": parsed.sheet or "",
            "notes": parsed.notes[:SHOWN],
            "rows": parsed.rows,
            "anchors": len(parsed.anchors),
            "new": [
                {
                    "keyword": r.keyword,
                    "url": r.url,
                    "page_type": r.page_type or "",
                    "naked_type": naked.get(r.keyword.casefold(), ""),
                }
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
            "deactivated_total": len(self.deactivate),
            "reactivated_total": len(self.reactivated),
            "naked_only": parsed.naked_only,
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
            "naked_total": len(parsed.naked),
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
            (self.anchor_list is None and (self.parsed.anchors or self.parsed.naked))
            or self.deactivate
            or self.new
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


def build_plan(
    parsed: Parsed, product: Product, *, sync: bool = False, region: Region | None = None
) -> AnchorPlan:
    if sync and (not parsed.anchors and not parsed.naked):
        raise ValueError("Полный список пуст: отключение анкоров не выполнено.")
    if sync and parsed.issues:
        raise ValueError(
            "В полном списке есть замечания. Исправьте файл или выберите дополнение списка."
        )
    region = region or region_of(product, None)
    anchor_list = AnchorList.objects.filter(product=product, country=region.country).first()
    plan = AnchorPlan(product=product, parsed=parsed, region=region, anchor_list=anchor_list)
    existing = _by_text(anchor_list)
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
            plan.reactivated.add(keyword.pk)
            values["is_active"] = True
            shown.append(("Активен", "нет", "да"))
        if shown:
            plan.changed.append(Change(keyword, values, shown))
        else:
            plan.same += 1
            if values:
                # Только тип «прямой» у ключа без типа — человеку это не новость.
                plan.quiet.append(Change(keyword, values, []))
    in_file.update(row.text.casefold() for row in parsed.naked)
    scope = [
        k
        for k in Keyword.objects.filter(anchor_list=anchor_list, is_active=True)
        if k.keyword.casefold() not in in_file
        and (
            (k.anchor_type in NAKED_TYPES and bool(parsed.naked))
            or (k.anchor_type not in NAKED_TYPES and not parsed.naked_only)
        )
    ]
    plan.missing = sorted(k.keyword for k in scope)
    plan.deactivate = scope if sync else []
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
            keyword_id__in=ids, country=plan.region.positions_country
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
        elif (
            (row.share is not None and keyword.share != row.share)
            or keyword.anchor_type not in NAKED_TYPES
            or not keyword.is_active
        ):
            plan.naked_changed.append((keyword, row))
            if not keyword.is_active:
                plan.reactivated.add(keyword.pk)
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
    """Сведение плана с базой — одной транзакцией. Итог — счётчики для экрана.

    Списка региона нет — запись его заводит (в журнале загрузки: отмена уберёт).
    """
    product = plan.product
    counts = {
        "created": 0,
        "updated": 0,
        "positions": 0,
        "naked": 0,
        "types": 0,
        "countries": 0,
        "links": 0,
        "deactivated": 0,
        "reactivated": len(plan.reactivated),
    }
    with transaction.atomic():
        anchor_list = (
            plan.anchor_list
            or AnchorList.objects.get_or_create(product=product, country=plan.region.country)[0]
        )
        plan.anchor_list = anchor_list
        created: dict[str, Keyword] = {}
        for row in plan.new:
            created[row.keyword.casefold()] = Keyword.objects.create(
                product=product,
                anchor_list=anchor_list,
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
        counts["positions"] = _write_positions(plan, created, anchor_list)
        counts["naked"] = _write_naked(plan, anchor_list)
        counts["types"] = _write_types(plan)
        counts["countries"] = _write_countries(plan)
        counts["deactivated"] = Keyword.objects.filter(
            anchor_list=anchor_list, pk__in=[k.pk for k in plan.deactivate], is_active=True
        ).update(is_active=False)
        counts["links"] = attach_links(anchor_list)
    return counts


def _write_positions(plan: AnchorPlan, created: dict[str, Keyword], anchor_list: AnchorList) -> int:
    existing = _by_text(anchor_list)
    written = 0
    for row in plan.parsed.anchors:
        keyword = created.get(row.keyword.casefold()) or existing.get(row.keyword.casefold())
        if keyword is None:
            continue
        for date, position in row.positions:
            _, made = KeywordPosition.objects.update_or_create(
                keyword=keyword,
                country=plan.region.positions_country,
                checked_at=date,
                defaults={"position": position, "source": MetricSource.CSV_IMPORT},
            )
            written += 1 if made else 0
    return written + plan.positions_changed


def _write_naked(plan: AnchorPlan, anchor_list: AnchorList) -> int:
    """Безанкорные анкоры: тип по тексту, тип страницы — как у ключа с тем же адресом."""
    page_types = _page_types_by_url(anchor_list)
    # CSV безанкорки и строки книги могут попасть и в основной лист анкоров.
    # Их уже записала первая часть этой транзакции: используем свежий список.
    existing = _by_text(anchor_list)
    written = 0
    for row in plan.naked_new:
        keyword = existing.get(row.text.casefold())
        if keyword is None:
            keyword = Keyword.objects.create(
                product=plan.product,
                anchor_list=anchor_list,
                keyword=row.text,
                target_url=row.url,
                anchor_type=naked_type(row.text, plan.product),
                page_type=page_types.get(_url_key(row.url)),
                share=row.share,
            )
            existing[row.text.casefold()] = keyword
        else:
            if row.share is not None:
                keyword.share = row.share
            keyword.anchor_type = naked_type(row.text, plan.product)
            keyword.page_type = keyword.page_type or page_types.get(_url_key(row.url))
            keyword.save(update_fields=["share", "anchor_type", "page_type"])
        written += 1
    for keyword, row in plan.naked_changed:
        fields = []
        if not keyword.is_active:
            keyword.is_active = True
            fields.append("is_active")
        if row.share is not None:
            keyword.share = row.share
            fields.append("share")
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


def attach_links(anchor_list: AnchorList) -> int:
    """Ссылки без анкора из списка — к анкору по тексту (ADR-069).

    Старые «Convertio», «click here» из таблицы получают анкор, когда в список
    пришла безанкорка; «Convertio's mp4 to mp3 converter» — когда пришёл ключ.
    Поиск — `apps.keywords.matching`: анкор целиком или ключ внутри фразы.
    Ссылки с ключом не трогаем: связь выбрал человек или прошлая загрузка.
    Только ссылки размещений этого списка (ADR-075); размещения без списка — у
    основного. Запись идёт в транзакции загрузки — журнал её отменит.
    """
    product_id = anchor_list.product_id
    pool = matching.candidates(product_id, anchor_list.pk)
    attached = 0
    own = Q(placement__anchor_list=anchor_list)
    if anchor_list == AnchorList.main_of(product_id):
        own |= Q(placement__anchor_list__isnull=True)
    links = PlacementLink.objects.filter(
        own, placement__product_id=product_id, keyword__isnull=True
    )
    for link in links.only("pk", "anchor", "target_url", "anchor_type"):
        found, kind = matching.bind(link.anchor, pool, url=link.target_url)
        if found.keyword is None:
            continue
        link.keyword_id = found.keyword.id
        fields = ["keyword"]
        if link.anchor_type is None:
            link.anchor_type = kind
            fields.append("anchor_type")
        link.save(update_fields=fields)
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


def _by_text(anchor_list: AnchorList | None) -> dict[str, Keyword]:
    if anchor_list is None:
        return {}
    return {k.keyword.casefold(): k for k in Keyword.objects.filter(anchor_list=anchor_list)}


def _page_types_by_url(anchor_list: AnchorList | None) -> dict[str, str]:
    found: dict[str, str] = {}
    for url, page_type in (
        Keyword.objects.filter(anchor_list=anchor_list, page_type__isnull=False)
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
