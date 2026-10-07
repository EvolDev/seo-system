"""Разметка колонок файла размещений: какое поле в какой колонке (E1-09, ADR-051).

Как у прайса (ADR-044): словарь синонимов только подсказывает, подтверждает
человек. Память — не у продавца, а по прошлым загрузкам размещений: файл
приходит от сотрудника, от продавца или без «от кого», а заголовки у этих
файлов одни и те же («Target», «Person», «Source», «Анкор1»…). Следующий файл
с теми же колонками проходит без вопросов, про новую колонку — один вопрос.

Поля — свои: «Цена» в прайсе — предложение продавца, а в файле размещений —
сколько заплатили. Поэтому и память отдельная от `sellers.column_map`.

Колонка индексации может быть не одна: «Индексация» и «Индексация 02.09.26» —
отметки на разные даты, каждая — своя проверка человеком.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from apps.sites.models import Upload, UploadKind, UploadStatus
from apps.sites.uploads.columns import Confidence, looks_like_header
from apps.sites.uploads.files import Column
from apps.sites.uploads.values import currency_in


class PField(StrEnum):
    """Куда записать колонку файла размещений. Значение хранится в разметке загрузки."""

    DOMAIN = "domain"
    ARTICLE_URL = "article_url"
    PUBLISHED = "published_at"
    ORDERED = "ordered_at"
    STATUS = "status"
    SERVICE = "service"
    SELLER = "seller"
    EMPLOYEE = "employee"
    ANCHOR_1 = "anchor_1"
    LINK_1 = "link_1"
    ANCHOR_2 = "anchor_2"
    LINK_2 = "link_2"
    ANCHOR_3 = "anchor_3"
    LINK_3 = "link_3"
    PAID = "price_paid"
    PRICE = "placement_price"
    WRITING = "writing_price"
    ANNOUNCE = "announce_price"
    INDEXED = "indexed"
    NOTE = "note"
    EXTRA = "extra"
    SKIP = "skip"

    @property
    def label(self) -> str:
        return FIELD_LABELS[self]


FIELD_LABELS: dict[PField, str] = {
    PField.DOMAIN: "Площадка (домен или адрес)",
    PField.ARTICLE_URL: "Адрес статьи",
    PField.PUBLISHED: "Дата размещения",
    PField.ORDERED: "Дата отправки заявки",
    PField.STATUS: "Статус (Размещено, Заявка отправлена…)",
    PField.SERVICE: "Тип размещения (публикация, вставка ссылки)",
    PField.SELLER: "Продавец — через кого куплено",
    PField.EMPLOYEE: "Сотрудник — кто вёл",
    PField.ANCHOR_1: "Анкор 1",
    PField.LINK_1: "Ссылка 1 — куда ведёт",
    PField.ANCHOR_2: "Анкор 2",
    PField.LINK_2: "Ссылка 2 — куда ведёт",
    PField.ANCHOR_3: "Анкор 3",
    PField.LINK_3: "Ссылка 3 — куда ведёт",
    PField.PAID: "Заплачено — итог",
    PField.PRICE: "Цена размещения — без написания и анонса",
    PField.WRITING: "Цена написания",
    PField.ANNOUNCE: "Цена анонса",
    PField.INDEXED: "Индексация (да / нет) — на дату из заголовка или дату файла",
    PField.NOTE: "Комментарий — в заметки площадки",
    PField.EXTRA: "Прочие данные",
    PField.SKIP: "Не загружать",
}

# Пары «анкор — ссылка» по номеру ссылки в статье.
LINK_PAIRS: tuple[tuple[int, PField, PField], ...] = (
    (1, PField.ANCHOR_1, PField.LINK_1),
    (2, PField.ANCHOR_2, PField.LINK_2),
    (3, PField.ANCHOR_3, PField.LINK_3),
)
PRICE_FIELDS = (PField.PAID, PField.PRICE, PField.WRITING, PField.ANNOUNCE)


@dataclass(frozen=True)
class Guess:
    field: PField
    confidence: Confidence
    hint: str = ""


_VALUES = frozenset(field.value for field in PField)
# Одна колонка на поле; прочие данные, «не загружать» и индексация — сколько угодно.
SINGLE_FIELDS = frozenset(PField) - {PField.EXTRA, PField.SKIP, PField.INDEXED}

_EXACT: dict[str, PField] = {
    **dict.fromkeys(
        (
            "target",
            "website",
            "websites",
            "web site",
            "site",
            "sites",
            "domain",
            "domains",
            "donor",
            "сайт",
            "сайты",
            "домен",
            "площадка",
            "площадки",
            "донор",
            "ресурс",
        ),
        PField.DOMAIN,
    ),
    **dict.fromkeys(
        (
            "url статьи",
            "адрес статьи",
            "ссылка на статью",
            "url публикации",
            "статья",
            "article",
            "article url",
            "article link",
            "url of article",
            "post url",
            "live link",
            "live url",
            "published url",
            "posted url",
        ),
        PField.ARTICLE_URL,
    ),
    **dict.fromkeys(
        (
            "дата размещения",
            "дата публикации",
            "дата",
            "date",
            "published",
            "publication date",
            "date published",
            "posted",
            "posted on",
        ),
        PField.PUBLISHED,
    ),
    # Дата отправки заявки вебмастеру — своя колонка (E1-23). «Дата» без
    # уточнения остаётся датой размещения: так её понимали все прежние файлы.
    **dict.fromkeys(
        (
            "дата отправки",
            "дата заявки",
            "дата отправки заявки",
            "отправлено",
            "sent",
            "sent on",
            "date sent",
            "ordered",
            "ordered on",
            "order date",
        ),
        PField.ORDERED,
    ),
    **dict.fromkeys(("статус", "status"), PField.STATUS),
    **dict.fromkeys(
        ("тип размещения", "тип", "type", "service", "услуга", "placement type", "тип услуги"),
        PField.SERVICE,
    ),
    **dict.fromkeys(
        (
            "seller",
            "продавец",
            "vendor",
            "reseller",
            "provider",
            "поставщик",
            "через кого",
        ),
        PField.SELLER,
    ),
    **dict.fromkeys(
        (
            "person",
            "сотрудник",
            "employee",
            "manager",
            "менеджер",
            "linkbuilder",
            "линкбилдер",
            "ответственный",
            "owner",
        ),
        PField.EMPLOYEE,
    ),
    **dict.fromkeys(
        (
            "итог цена",
            "итог",
            "итого",
            "цена итог",
            "заплачено",
            "оплачено",
            "paid",
            "price paid",
            "total",
            "total price",
        ),
        PField.PAID,
    ),
    **dict.fromkeys(
        (
            "цена размещ",
            "цена размещения",
            "цена публикации",
            "publication price",
            "placement price",
            "gp price",
            "guest post price",
        ),
        PField.PRICE,
    ),
    **dict.fromkeys(
        ("цена написания", "написание", "writing", "writing price", "content price"),
        PField.WRITING,
    ),
    **dict.fromkeys(("цена анонса", "анонс", "announce", "announcement"), PField.ANNOUNCE),
    **dict.fromkeys(
        (
            "комментарий",
            "комментарии",
            "comment",
            "comments",
            "примечание",
            "заметка",
            "note",
            "notes",
        ),
        PField.NOTE,
    ),
}
_ANCHOR = re.compile(r"^(?:анкор|anchor|anchor text)\s*(\d?)$")
_LINK = re.compile(r"^(?:ссылка|link|target link|target url|landing|landing page)\s*(\d?)$")
_INDEXED = re.compile(r"^(?:индексация|в индексе|индекс|indexation|indexed|index)\b")
_PRICE = {"цена", "price", "стоимость", "сумма", "cost"}
# Метрики площадки в файле размещений — снятые неизвестно когда: в прочие данные.
_METRIC_TOKENS = {
    "traffic",
    "traf",
    "трафик",
    "dr",
    "keywords",
    "ключи",
    "ключей",
    "geo",
    "гео",
    "da",
}
_TOKEN = re.compile(r"[a-zа-яё0-9]+")
_HTTP = re.compile(r"^https?://[^/\s]+/\S+", re.IGNORECASE)


def guess_columns(columns: Sequence[Column]) -> dict[str, Guess]:
    """Догадка по каждой колонке: поле и уверенность. Ключ — `Column.key`."""
    guesses = {column.key: _guess(column) for column in columns}
    has_domain = any(guess.field == PField.DOMAIN for guess in guesses.values())
    for column in columns:
        guess = guesses[column.key]
        if column.key == "url":
            # «URL» рядом с колонкой площадки — адрес статьи, без неё — сама площадка.
            field = PField.ARTICLE_URL if has_domain else PField.DOMAIN
            guesses[column.key] = Guess(field, Confidence.LIKELY)
        elif guess.confidence == Confidence.UNKNOWN and _all_article_urls(column):
            guesses[column.key] = Guess(PField.ARTICLE_URL, Confidence.LIKELY)
    return _one_column_per_field(columns, guesses)


def build_mapping(
    columns: Sequence[Column], remembered: Mapping[str, str] | None
) -> tuple[dict[str, Guess], list[str]]:
    """Разметка файла: запомненное по прошлым загрузкам + догадки. Второе — о чём спросить."""
    guesses = guess_columns(columns)
    remembered = remembered or {}
    questions: list[str] = []
    for column in columns:
        known = remembered.get(column.key)
        if known in _VALUES:
            guesses[column.key] = Guess(PField(known), Confidence.REMEMBERED)
        elif not column.is_empty:
            questions.append(column.key)
    return guesses, questions


def validate_mapping(mapping: Mapping[str, PField], columns: Sequence[Column]) -> list[str]:
    """Ошибки разметки человеческим языком; пусто — разметка годится."""
    errors: list[str] = []
    headers = {column.key: column.header for column in columns}
    by_field: dict[PField, list[str]] = {}
    for key, field in mapping.items():
        by_field.setdefault(field, []).append(headers.get(key, key))
    if PField.DOMAIN not in by_field:
        errors.append("Укажите колонку с площадкой — доменом или адресом сайта.")
    for _, anchor, link in LINK_PAIRS:
        if anchor in by_field and link not in by_field:
            errors.append(
                f"«{anchor.label}» без «{link.label}»: ссылка без адреса не записывается."
            )
    for field in sorted(SINGLE_FIELDS, key=list(PField).index):
        if len(by_field.get(field, [])) > 1:
            names = ", ".join(f"«{name}»" for name in by_field[field])
            errors.append(f"«{field.label}» выбрано у нескольких колонок: {names}.")
    return errors


def remembered_mapping() -> dict[str, str]:
    """Разметка прошлых загрузок размещений: заголовок → поле, свежая перекрывает старую.

    Берутся загрузки, где разметку подтвердил человек: дошедшие до проверки.
    """
    remembered: dict[str, str] = {}
    confirmed = (UploadStatus.CHECKING, UploadStatus.CHECKED, UploadStatus.WRITING)
    uploads = (
        Upload.objects.filter(kind=UploadKind.PLACEMENTS, mapping__isnull=False)
        .filter(status__in=(*confirmed, UploadStatus.DONE))
        .order_by("created_at", "pk")
        .values_list("mapping", flat=True)
    )
    for mapping in uploads:
        remembered.update(_known_values(mapping))
    return remembered


def detect_currency(
    columns: Sequence[Column], mapping: Mapping[str, PField], default: str = "EUR"
) -> tuple[str, str]:
    """Валюта цен файла и откуда она взялась: заголовок, значения или евро по умолчанию."""
    priced = [column for column in columns if mapping.get(column.key) in PRICE_FIELDS]
    for column in priced:
        found = currency_in(column.header)
        if found:
            return found, f"в заголовке «{column.header}»"
    for column in priced:
        for sample in column.samples:
            found = currency_in(sample)
            if found:
                return found, f"в значениях колонки «{column.header}»"
    return default, "евро по умолчанию"


def looks_like_placements_header(cells: Sequence[str]) -> bool:
    """Строка заголовков: есть колонка площадки по словарю прайсов или файла размещений."""
    return looks_like_header(cells) or any(_clean(cell) in _EXACT for cell in cells)


def _known_values(mapping: Any) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    return {str(key): str(value) for key, value in mapping.items() if value in _VALUES}


def _clean(header: str) -> str:
    """Заголовок без регистра, знаков валюты и пунктуации по краям: «Цена, $» → «цена»."""
    cleaned = header.replace("\xa0", " ").lower()
    cleaned = re.sub(r"[$€£₴₽]", " ", cleaned)
    cleaned = re.sub(r"\((usd|eur|gbp|uah|rub)\)|\b(usd|eur|gbp|uah|rub)\b", " ", cleaned)
    return " ".join(cleaned.split()).strip(" ,.:;-_()")


def _guess(column: Column) -> Guess:
    key = _clean(column.key)
    exact = _EXACT.get(key)
    if exact is not None:
        return Guess(exact, Confidence.SURE)
    anchor = _ANCHOR.match(key)
    if anchor:
        return _numbered(anchor.group(1), (PField.ANCHOR_1, PField.ANCHOR_2, PField.ANCHOR_3))
    link = _LINK.match(key)
    if link:
        return _numbered(link.group(1), (PField.LINK_1, PField.LINK_2, PField.LINK_3))
    if _INDEXED.match(key):
        return Guess(PField.INDEXED, Confidence.SURE)
    if key == "source":
        return Guess(
            PField.SELLER,
            Confidence.LIKELY,
            "Source здесь — через кого куплено? Если это откуда площадка — «Прочие данные».",
        )
    if key in _PRICE:
        return Guess(PField.PAID, Confidence.LIKELY, "Цена в файле размещений — сколько заплатили.")
    tokens = set(_TOKEN.findall(key))
    if tokens & _METRIC_TOKENS:
        return Guess(
            PField.EXTRA,
            Confidence.LIKELY,
            "Метрики площадки из файла — в прочие данные: когда их сняли, неизвестно.",
        )
    return Guess(PField.EXTRA, Confidence.UNKNOWN)


def _numbered(number: str, fields: tuple[PField, PField, PField]) -> Guess:
    index = int(number) if number else 1
    if not 1 <= index <= len(fields):
        return Guess(PField.EXTRA, Confidence.UNKNOWN, "Больше трёх ссылок — в прочие данные.")
    return Guess(fields[index - 1], Confidence.SURE if number else Confidence.LIKELY)


def _all_article_urls(column: Column) -> bool:
    return bool(column.samples) and all(_HTTP.match(sample) for sample in column.samples)


def _one_column_per_field(columns: Sequence[Column], guesses: dict[str, Guess]) -> dict[str, Guess]:
    """Одно поле — одна колонка: из нескольких кандидатов остаётся точная или первая."""
    by_field: dict[PField, list[Column]] = {}
    for column in columns:
        field = guesses[column.key].field
        if field in SINGLE_FIELDS:
            by_field.setdefault(field, []).append(column)
    for candidates in by_field.values():
        if len(candidates) < 2:
            continue
        best = min(
            candidates,
            key=lambda c: (guesses[c.key].confidence != Confidence.SURE, c.index),
        )
        for column in candidates:
            if column is not best:
                guesses[column.key] = Guess(PField.EXTRA, Confidence.UNKNOWN)
    return guesses
