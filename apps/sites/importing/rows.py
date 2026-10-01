"""Разбор вкладок в данные для записи (маппинг §1–2). Базы здесь нет.

Всё, что не разобралось, уходит в отчёт, а поле остаётся пустым: одна
битая ячейка не должна останавливать импорт двух тысяч строк.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, fields, replace

from apps.placements.models import PlacementStatus
from apps.sites.domains import normalize_domain
from apps.sites.importing import values
from apps.sites.importing.languages import language_code
from apps.sites.importing.report import Report, Section
from apps.sites.importing.workbook import Sheet, SheetRow, position_columns
from apps.sites.models import PlacementType

# Основная вкладка «База линкбилдинга».
BASE_SHEET = "База линкбилдинга"
TARGET = "Target"
SOURCE = "Источник"
SITE_COMMENT = "Комментарий к площадке"
ARTICLE_URL = "URL статьи"
INDEXED = "Индексация"
STATUS = "Статус"
PUBLISHED = "Дата размещения"
PLACEMENT_COMMENT = "Комментарий по размещению"
ANCHOR_COLUMNS = (("Анкор1", "Ссылка1", 1), ("Анкор2", "Ссылка2", 2))
CLIDEO = "Пример статьи на Clideo"
TRAFFIC = "Organic / Traffic"
TOP_GEO = "Top Geo"
TOP_GEO_TRAFFIC = "Top Geo Traff"
US_TRAFFIC = "US Traff"
DR = "DR"
KEYWORDS_TOTAL = "Organic / Total Keywords"
# «Тип ссылки» — формат размещения, «Тип ссылки статья» — dofollow/nofollow.
PLACEMENT_TYPE = "Тип ссылки"
LINK_TYPE = "Тип ссылки статья"
PRICE_PLACEMENT = "Цена размещения статья, EUR"
PRICE_ANNOUNCE = "Цена анонса статья, EUR"
PRICE_WRITING = "Цена написания статья, EUR"
TOTAL_PRICE = "Итог цена"  # не импортируется, только счётчик в отчёт
LINKS_ALLOWED = "Количество ссылок статья"
MARKS_AS_AD = "Пометка о рекламе статья"
DECLARED_TOPICS = "Особые тематики"
LANGUAGES = "Языки сайта"
SITE_TYPE = "Тип сайта"
COLLABORATOR_URL = "URL Коллаборатора"
TOPICS = "Тематика"

BASE_REQUIRED = (
    TARGET,
    SOURCE,
    SITE_COMMENT,
    ARTICLE_URL,
    INDEXED,
    STATUS,
    PUBLISHED,
    PLACEMENT_COMMENT,
    *(column for pair in ANCHOR_COLUMNS for column in pair[:2]),
    CLIDEO,
    TRAFFIC,
    TOP_GEO,
    TOP_GEO_TRAFFIC,
    US_TRAFFIC,
    DR,
    KEYWORDS_TOTAL,
    PLACEMENT_TYPE,
    LINK_TYPE,
    PRICE_PLACEMENT,
    PRICE_ANNOUNCE,
    PRICE_WRITING,
    LINKS_ALLOWED,
    MARKS_AS_AD,
    DECLARED_TOPICS,
    LANGUAGES,
    SITE_TYPE,
    COLLABORATOR_URL,
    TOPICS,
)

# Вкладка «Распределение анкоров».
KEYWORDS_SHEET = "Распределение анкоров"
KEYWORD = "Keyword"
KEYWORD_URL = "URL"
VOLUME = "Volume"
GLOBAL_VOLUME = "Global Volume"
LINKS_PLACED = "Links Placed"
LINKS_WAITING = "Links Waiting"
TOOL = "Tool"
PAGE_TYPE = "Type"
KEYWORDS_REQUIRED = (KEYWORD, KEYWORD_URL, VOLUME, GLOBAL_VOLUME, TOOL, PAGE_TYPE)

# Вкладка «Размещения» — ручная копия, только для сверки (маппинг §1.8).
COPY_SHEET = "Размещения"
COPY_REQUIRED = (TARGET, STATUS, ARTICLE_URL, PUBLISHED, INDEXED, PLACEMENT_TYPE)

PLACEMENT_STATUSES = {
    "Размещено": PlacementStatus.PUBLISHED,
    "Заявка отправлена": PlacementStatus.ORDERED,
}
PLACEMENT_TYPES = {
    "Guest Post": PlacementType.GUEST_POST,
    "Link Insertion": PlacementType.LINK_INSERTION,
}

# Разборщик ячейки из `values`: значение → результат или ValueError.
# `type X[T] = …` — псевдоним типа с параметром (Python 3.12).
type Parser[T] = Callable[[object], T | None]


@dataclass(frozen=True)
class Card:
    """Факты карточки Collaborator → `sites`. Имена полей — как в модели."""

    source: str | None
    language: str | None
    languages: list[str] | None
    topics: list[str] | None
    declared_topics: list[str] | None
    site_type: str | None
    collaborator_url: str | None
    links_allowed: int | None
    link_type: str | None
    marks_as_ad: bool | None

    def as_fields(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class Metrics:
    dr: int | None
    organic_traffic: int | None
    us_traffic: int | None
    top_geo: str | None
    top_geo_traffic: int | None
    total_keywords: int | None

    def as_fields(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def is_empty(self) -> bool:
        return all(value is None for value in self.as_fields().values())


@dataclass(frozen=True)
class Prices:
    placement_cents: int | None
    announce_cents: int | None
    writing_cents: int | None

    def as_fields(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def is_empty(self) -> bool:
        return all(value is None for value in self.as_fields().values())


@dataclass(frozen=True)
class Link:
    index: int
    anchor: str
    target_url: str


@dataclass(frozen=True)
class PlacementData:
    """Размещение Convertio из строки основной вкладки (маппинг §1.5)."""

    status: PlacementStatus
    article_url: str | None
    published_on: dt.date | None
    is_indexed: bool | None
    placement_type: PlacementType | None
    comment: str | None
    links: tuple[Link, ...]


@dataclass(frozen=True)
class CopyRow:
    """Строка вкладки «Размещения» в том виде, в каком её сверяют (маппинг §1.8)."""

    row: int
    domain: str
    status: str | None
    article_url: str | None
    published_on: dt.date | str | None
    is_indexed: bool | str | None
    placement_type: str | None


@dataclass(frozen=True)
class SiteData:
    row: int
    domain: str
    card: Card
    comment: str | None  # «Комментарий к площадке»: отказ или заметка
    metrics: Metrics
    prices: Prices
    placement: PlacementData | None
    clideo_url: str | None
    placement_cells: CopyRow  # те же колонки, что сверяются с «Размещениями»

    @property
    def where(self) -> str:
        return f"{self.domain} (строка {self.row})"


@dataclass(frozen=True)
class KeywordData:
    row: int
    keyword: str
    target_url: str | None
    volume: int | None
    global_volume: int | None
    tool: str | None
    page_type: str | None
    links_placed: int | None  # колонки-формулы таблицы — только для сверки
    links_waiting: int | None
    positions: dict[dt.date, int | None]  # пустой ячейки здесь нет; None — «вне топ-100»


class _Cells:
    """Ячейки одной строки: разбирает значение, ошибку пишет в отчёт."""

    def __init__(self, row: SheetRow, where: str, report: Report) -> None:
        self.row = row
        self.where = where
        self.report = report

    def text(self, column: str) -> str | None:
        return values.text(self.row.get(column))

    def number(self, column: str) -> int | None:
        return self._parse(column, values.parse_int)

    def cents(self, column: str) -> int | None:
        return self._parse(column, values.parse_cents)

    def announce_cents(self, column: str) -> int | None:
        return self._parse(column, values.parse_announce_cents)

    def yes_no(self, column: str) -> bool | None:
        return self._parse(column, values.parse_yes_no)

    def date(self, column: str) -> dt.date | None:
        return self._parse(column, values.parse_date)

    def _parse[T](self, column: str, parser: Parser[T]) -> T | None:
        # `def _parse[T]` — обобщённая функция: что вернёт разборщик, то
        # вернёт и она, mypy проверяет это по типам.
        value = self.row.get(column)
        try:
            return parser(value)
        except ValueError as error:
            self.report.issue(Section.BAD_VALUES, f"{self.where}: «{column}» — {error}")
            return None


def parse_base(sheet: Sheet, report: Report) -> list[SiteData]:
    """Основная вкладка: одна `SiteData` на домен, дубли слиты (маппинг §1.1)."""
    first_rows: dict[str, SheetRow] = {}
    result: list[SiteData] = []
    merged = 0
    for row in sheet.rows:
        target = values.text(row.get(TARGET))
        if target is None:
            report.issue(Section.SKIPPED_ROWS, f"строка {row.number}: нет Target")
            continue
        try:
            domain = normalize_domain(target)
        except ValueError:
            report.issue(Section.SKIPPED_ROWS, f"строка {row.number}: не домен — {target!r}")
            continue
        first = first_rows.get(domain)
        if first is not None:
            if first.values == row.values:
                merged += 1
            else:
                report.issue(Section.DUPLICATES, _duplicate_line(domain, first, row))
            continue
        first_rows[domain] = row
        result.append(_parse_site(row, domain, report))
    if sheet.blank_rows:
        report.note(f"«{sheet.name}»: пустых строк пропущено — {sheet.blank_rows}")
    if merged:
        report.note(f"«{sheet.name}»: одинаковых строк слито — {merged}")
    return result


def _duplicate_line(domain: str, first: SheetRow, other: SheetRow) -> str:
    differences = [
        f"«{column}»: {first.get(column)!r} / {other.get(column)!r}"
        for column in first.values
        if first.get(column) != other.get(column)
    ]
    return (
        f"{domain}: строки {first.number} и {other.number} различаются — "
        f"{'; '.join(differences)}. Взята строка {first.number}"
    )


def _parse_site(row: SheetRow, domain: str, report: Report) -> SiteData:
    where = f"{domain} (строка {row.number})"
    cells = _Cells(row, where, report)
    site = SiteData(
        row=row.number,
        domain=domain,
        card=_parse_card(cells, domain, report),
        comment=cells.text(SITE_COMMENT),
        metrics=_parse_metrics(cells, where, report),
        prices=Prices(
            placement_cents=cells.cents(PRICE_PLACEMENT),
            announce_cents=cells.announce_cents(PRICE_ANNOUNCE),
            writing_cents=cells.cents(PRICE_WRITING),
        ),
        placement=_parse_placement(cells, where, report),
        clideo_url=cells.text(CLIDEO),
        placement_cells=_copy_view(row, domain),
    )
    if not values.is_blank(row.get(PRICE_PLACEMENT)) and _is_zero(row.get(TOTAL_PRICE)):
        report.issue(Section.TOTAL_PRICE_ZERO, where)
    return site


def _is_zero(value: object) -> bool:
    return isinstance(value, int | float) and value == 0


def _parse_card(cells: _Cells, domain: str, report: Report) -> Card:
    languages = values.split_languages(cells.row.get(LANGUAGES))
    language = None
    if languages:
        language = language_code(languages[0])
        if language is None:
            report.issue(Section.UNKNOWN_LANGUAGE, f"{cells.where}: «{languages[0]}»")
    return Card(
        source=cells.text(SOURCE),
        language=language,
        languages=languages or None,
        topics=values.split_categories(cells.row.get(TOPICS)) or None,
        declared_topics=values.split_categories(cells.row.get(DECLARED_TOPICS)) or None,
        site_type=cells.text(SITE_TYPE),
        collaborator_url=cells.text(COLLABORATOR_URL),
        links_allowed=cells.number(LINKS_ALLOWED),
        link_type=cells.text(LINK_TYPE),
        marks_as_ad=cells.yes_no(MARKS_AS_AD),
    )


def _parse_metrics(cells: _Cells, where: str, report: Report) -> Metrics:
    metrics = Metrics(
        dr=cells.number(DR),
        organic_traffic=cells.number(TRAFFIC),
        us_traffic=cells.number(US_TRAFFIC),
        top_geo=cells.text(TOP_GEO),
        top_geo_traffic=cells.number(TOP_GEO_TRAFFIC),
        total_keywords=cells.number(KEYWORDS_TOTAL),
    )
    no_ahrefs = (
        metrics.organic_traffic is None
        and metrics.total_keywords is None
        and metrics.top_geo is None
        and metrics.top_geo_traffic is None
    )
    if no_ahrefs and metrics.us_traffic == 0:
        # Трафика, ключей и гео нет, а US Traff = 0 — это «нет данных»,
        # а не ноль трафика (маппинг §1.3).
        report.issue(Section.NO_AHREFS, where)
        metrics = replace(metrics, us_traffic=None)
    if (
        metrics.top_geo is not None
        and metrics.top_geo.lower() != "us"
        and metrics.us_traffic
        and metrics.us_traffic == metrics.top_geo_traffic
    ):
        report.issue(
            Section.US_EQUALS_GEO,
            f"{where}: гео {metrics.top_geo}, US Traff = Top Geo Traff = {metrics.us_traffic}",
        )
    return metrics


def _parse_placement(cells: _Cells, where: str, report: Report) -> PlacementData | None:
    """Размещение Convertio, если в строке есть статус, URL, дата или анкор."""
    status_text = cells.text(STATUS)
    links = _parse_links(cells, where, report)
    article_url = cells.text(ARTICLE_URL)
    published_on = cells.date(PUBLISHED)
    if status_text is None and not (links or article_url or published_on):
        return None
    if status_text is None:
        status = PlacementStatus.PLANNED
    elif status_text in PLACEMENT_STATUSES:
        status = PLACEMENT_STATUSES[status_text]
    else:
        anchors = ", ".join(link.anchor for link in links) or "нет"
        report.issue(Section.UNKNOWN_STATUS, f"{where}: «{status_text}», анкоры: {anchors}")
        return None
    type_text = cells.text(PLACEMENT_TYPE)
    placement_type = PLACEMENT_TYPES.get(type_text) if type_text else None
    if type_text and placement_type is None:
        report.issue(Section.BAD_VALUES, f"{where}: «{PLACEMENT_TYPE}» — {type_text!r}")
    placement = PlacementData(
        status=status,
        article_url=article_url,
        published_on=published_on,
        is_indexed=cells.yes_no(INDEXED),
        placement_type=placement_type,
        comment=cells.text(PLACEMENT_COMMENT),
        links=tuple(links),
    )
    site_comment = cells.text(SITE_COMMENT)
    for comment in (site_comment, placement.comment):
        if values.mentions_refusal(comment):
            report.issue(
                Section.REFUSALS,
                f"{where}: статус в таблице «{status_text or 'пусто'}», комментарий: {comment}",
            )
            break
    return placement


def _parse_links(cells: _Cells, where: str, report: Report) -> list[Link]:
    links = []
    for anchor_column, url_column, index in ANCHOR_COLUMNS:
        anchor = cells.text(anchor_column)
        url = cells.text(url_column)
        if anchor and url:
            links.append(Link(index, anchor, url))
        elif anchor or url:
            report.issue(
                Section.SKIPPED_ROWS,
                f"{where}: ссылка {index} без анкора или адреса — {anchor!r}, {url!r}",
            )
    return links


def parse_keywords(sheet: Sheet, report: Report) -> list[KeywordData]:
    """Вкладка анкоров: ключи и позиции по колонкам «Pos ДД.ММ.ГГ» (маппинг §2)."""
    dates = position_columns(sheet.headers)
    if not dates:
        report.note(f"«{sheet.name}»: колонок позиций «Pos ДД.ММ.ГГ» нет")
    result: list[KeywordData] = []
    seen: set[str] = set()
    for row in sheet.rows:
        keyword = values.text(row.get(KEYWORD))
        if keyword is None:
            report.issue(Section.SKIPPED_ROWS, f"«{sheet.name}», строка {row.number}: нет Keyword")
            continue
        if keyword in seen:
            report.issue(Section.DUPLICATES, f"ключ «{keyword}» повторяется: строка {row.number}")
            continue
        seen.add(keyword)
        cells = _Cells(row, f"ключ «{keyword}» (строка {row.number})", report)
        positions: dict[dt.date, int | None] = {}
        for column, day in dates.items():
            value = row.get(column)
            if values.is_blank(value):
                continue
            try:
                positions[day] = values.parse_position(value)
            except ValueError as error:
                report.issue(Section.BAD_VALUES, f"{cells.where}: «{column}» — {error}")
        result.append(
            KeywordData(
                row=row.number,
                keyword=keyword,
                target_url=cells.text(KEYWORD_URL),
                volume=cells.number(VOLUME),
                global_volume=cells.number(GLOBAL_VOLUME),
                tool=cells.text(TOOL),
                page_type=cells.text(PAGE_TYPE),
                links_placed=cells.number(LINKS_PLACED),
                links_waiting=cells.number(LINKS_WAITING),
                positions=positions,
            )
        )
    if sheet.blank_rows:
        report.note(f"«{sheet.name}»: пустых строк пропущено — {sheet.blank_rows}")
    return result


def parse_copy(sheet: Sheet, report: Report) -> list[CopyRow]:
    """Вкладка «Размещения»: значения для сверки, без записи в базу."""
    result = []
    for row in sheet.rows:
        target = values.text(row.get(TARGET))
        if target is None:
            continue
        try:
            domain = normalize_domain(target)
        except ValueError:
            report.issue(Section.COPY_MISMATCH, f"строка {row.number}: не домен — {target!r}")
            continue
        result.append(_copy_view(row, domain))
    return result


def _copy_view(row: SheetRow, domain: str) -> CopyRow:
    return CopyRow(
        row=row.number,
        domain=domain,
        status=values.text(row.get(STATUS)),
        article_url=values.text(row.get(ARTICLE_URL)),
        published_on=_lenient(values.parse_date, row.get(PUBLISHED)),
        is_indexed=_lenient(values.parse_yes_no, row.get(INDEXED)),
        placement_type=values.text(row.get(PLACEMENT_TYPE)),
    )


def _lenient[T](parser: Parser[T], value: object) -> T | str | None:
    """Разобранное значение, а если не разобралось — текст как есть, для сверки."""
    try:
        return parser(value)
    except ValueError:
        return values.text(value)
