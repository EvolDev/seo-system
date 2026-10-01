"""Разметка колонок прайса: какое поле в какой колонке (ADR-041, ADR-044).

Колонки угадываются по словарю синонимов, но словарь только подсказывает:
у разных продавцов одно название значит разное («Traffic» — органический
или общий). Подтверждает человек, разметка запоминается у продавца: в
следующий раз спрашиваем только про новые колонки.

Непустая колонка без своего поля по умолчанию идёт в «прочие данные»;
«не загружать» — только явным выбором.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from apps.sites.uploads.files import Column, header_key
from apps.sites.uploads.values import currency_in


class Field(StrEnum):
    """Куда записать колонку. Значение хранится в разметке продавца."""

    DOMAIN = "domain"
    GUEST_POST = "guest_post_price"
    LINK_INSERTION = "link_insertion_price"
    PRICE = "price"
    SERVICE = "service"
    GRAY = "gray_price"
    WRITING = "writing_price"
    ANNOUNCE = "announce_price"
    DR = "dr"
    TRAFFIC = "organic_traffic"
    KEYWORDS = "total_keywords"
    LINK_TYPE = "link_type"
    NOTE = "note"
    EXTRA = "extra"
    SKIP = "skip"

    @property
    def label(self) -> str:
        return FIELD_LABELS[self]


FIELD_LABELS: dict[Field, str] = {
    Field.DOMAIN: "Площадка (домен или адрес)",
    Field.GUEST_POST: "Цена публикации",
    Field.LINK_INSERTION: "Цена вставки ссылки",
    Field.PRICE: "Цена услуги из колонки «Услуга»",
    Field.SERVICE: "Услуга (публикация, вставка, Both)",
    Field.GRAY: "Серая цена",
    Field.WRITING: "Цена написания",
    Field.ANNOUNCE: "Цена анонса",
    Field.DR: "DR",
    Field.TRAFFIC: "Органический трафик",
    Field.KEYWORDS: "Ключей в органике",
    Field.LINK_TYPE: "Тип ссылки (dofollow / nofollow)",
    Field.NOTE: "Наша заметка — в историю заметок",
    Field.EXTRA: "Прочие данные",
    Field.SKIP: "Не загружать",
}

PRICE_FIELDS = (Field.GUEST_POST, Field.LINK_INSERTION, Field.PRICE)
_FIELD_VALUES = frozenset(field.value for field in Field)
# Эти поля — одна колонка на файл; «прочие данные» и «не загружать» — сколько угодно.
SINGLE_FIELDS = frozenset(Field) - {Field.EXTRA, Field.SKIP}


class Confidence(StrEnum):
    SURE = "точно"
    LIKELY = "похоже — проверьте"
    UNKNOWN = "не знаю → прочие данные"
    REMEMBERED = "запомнено у продавца"
    CHOSEN = "вы выбрали"


@dataclass(frozen=True)
class Guess:
    field: Field
    confidence: Confidence
    hint: str = ""


# Точные заголовки (после header_key, без знаков валюты и пунктуации по краям).
_EXACT: dict[str, Field] = {
    **dict.fromkeys(
        (
            "website",
            "websites",
            "web site",
            "site",
            "sites",
            "domain",
            "domains",
            "domain name",
            "url",
            "site url",
            "website url",
            "target",
            "donor",
            "сайт",
            "сайты",
            "домен",
            "площадка",
            "площадки",
            "донор",
            "ресурс",
            "адрес сайта",
        ),
        Field.DOMAIN,
    ),
    **dict.fromkeys(
        (
            "gp price",
            "guest post price",
            "guest post",
            "guest posts",
            "gp",
            "price gp",
            "price guest post",
            "article price",
            "post price",
            "sponsored post price",
            "цена публикации",
            "публикация",
            "цена статьи",
            "статья",
            "гостевой пост",
        ),
        Field.GUEST_POST,
    ),
    **dict.fromkeys(
        (
            "li price",
            "link insertion price",
            "link insertion",
            "li",
            "price li",
            "niche edit",
            "niche edit price",
            "insertion price",
            "цена вставки",
            "вставка",
            "вставка ссылки",
            "цена вставки ссылки",
        ),
        Field.LINK_INSERTION,
    ),
    **dict.fromkeys(
        (
            "grey price",
            "gray price",
            "grey niche price",
            "gray niche price",
            "casino price",
            "cbd price",
            "sensitive price",
            "серая цена",
            "цена серой",
        ),
        Field.GRAY,
    ),
    **dict.fromkeys(
        ("type", "placement type", "service", "услуга", "тип размещения", "тип услуги"),
        Field.SERVICE,
    ),
    **dict.fromkeys(("dr", "ahrefs dr", "domain rating"), Field.DR),
    **dict.fromkeys(
        ("link type", "rel", "rel attribute", "тип ссылки", "dofollow/nofollow", "follow"),
        Field.LINK_TYPE,
    ),
}
# Цена без указания услуги: публикация, а если в файле есть колонка услуги — её цена.
_GENERIC_PRICE = {"price", "цена", "cost", "стоимость", "rate", "price per post"}
_TOKEN = re.compile(r"[a-zа-яё0-9]+")
# Заголовок про цену у Collaborator в прайсе продавца — справочная пометка коллеги.
_FOREIGN = ("collab", "коллаб", "коллоб")


def guess_columns(columns: Sequence[Column]) -> dict[str, Guess]:
    """Догадка по каждой колонке: поле и уверенность. Ключ — `Column.key`."""
    tokens = {column.key: _tokens(column.header) for column in columns}
    has_service = any(_exact_field(column.key) == Field.SERVICE for column in columns)
    guesses: dict[str, Guess] = {}
    for column in columns:
        guesses[column.key] = _guess(column, tokens[column.key], has_service=has_service)
    return _one_column_per_field(columns, guesses, tokens)


def build_mapping(
    columns: Sequence[Column], remembered: Mapping[str, str] | None
) -> tuple[dict[str, Guess], list[str]]:
    """Разметка файла: запомненное у продавца + догадки. Второе — о чём спросить.

    Спрашиваем про непустые колонки, которых продавец ещё не присылал.
    Пустая колонка — ни поля, ни вопроса: записывать из неё нечего.
    """
    guesses = guess_columns(columns)
    remembered = remembered or {}
    questions: list[str] = []
    for column in columns:
        known = remembered.get(column.key)
        if known in _FIELD_VALUES:
            guesses[column.key] = Guess(Field(known), Confidence.REMEMBERED)
        elif not column.is_empty:
            questions.append(column.key)
    return guesses, questions


def validate_mapping(mapping: Mapping[str, Field], columns: Sequence[Column]) -> list[str]:
    """Ошибки разметки человеческим языком; пусто — разметка годится."""
    errors: list[str] = []
    headers = {column.key: column.header for column in columns}
    by_field: dict[Field, list[str]] = {}
    for key, field in mapping.items():
        by_field.setdefault(field, []).append(headers.get(key, key))
    if Field.DOMAIN not in by_field:
        errors.append("Укажите колонку с площадкой — доменом или адресом сайта.")
    if not any(field in by_field for field in PRICE_FIELDS):
        errors.append("Укажите хотя бы одну колонку с ценой.")
    if (Field.PRICE in by_field) != (Field.SERVICE in by_field):
        errors.append(
            "«Цена услуги из колонки «Услуга»» и «Услуга» выбираются вместе: "
            "услуга говорит, за что эта цена."
        )
    for field in sorted(SINGLE_FIELDS, key=list(Field).index):
        if len(by_field.get(field, [])) > 1:
            names = ", ".join(f"«{name}»" for name in by_field[field])
            errors.append(f"«{field.label}» выбрано у нескольких колонок: {names}.")
    return errors


def detect_currency(
    columns: Sequence[Column], mapping: Mapping[str, Field], default: str
) -> tuple[str, str]:
    """Валюта цен файла и откуда она взялась — для подсказки человеку.

    Сначала заголовки колонок с ценами («Цена, $»), потом знаки в первых
    значениях («$170.00»), иначе — валюта продавца по умолчанию.
    """
    priced = [
        column
        for column in columns
        if mapping.get(column.key) in (*PRICE_FIELDS, Field.GRAY, Field.WRITING, Field.ANNOUNCE)
    ]
    for column in priced:
        found = currency_in(column.header)
        if found:
            return found, f"в заголовке «{column.header}»"
    for column in priced:
        for sample in column.samples:
            found = currency_in(sample)
            if found:
                return found, f"в значениях колонки «{column.header}»"
    return default, "валюта продавца по умолчанию"


def looks_like_header(cells: Iterable[str]) -> bool:
    """Строка заголовков: есть колонка площадки по словарю."""
    return any(_exact_field(header_key(cell)) == Field.DOMAIN for cell in cells)


def _clean(key: str) -> str:
    """Ключ без знаков валюты и пунктуации по краям: «цена, $» → «цена»."""
    cleaned = re.sub(r"[$€£₴₽]", " ", key)
    cleaned = re.sub(r"\((usd|eur|gbp|uah|rub)\)|\b(usd|eur|gbp|uah|rub)\b", " ", cleaned)
    return " ".join(cleaned.strip(" ,.:;-_()").split()).strip(" ,.:;-_()")


def _exact_field(key: str) -> Field | None:
    return _EXACT.get(_clean(key))


def _tokens(header: str) -> set[str]:
    return set(_TOKEN.findall(header.lower()))


def _guess(column: Column, tokens: set[str], *, has_service: bool) -> Guess:
    key = column.key
    if any(mark in key for mark in _FOREIGN):
        return Guess(Field.EXTRA, Confidence.UNKNOWN)
    exact = _exact_field(key)
    if exact is not None:
        return Guess(exact, Confidence.SURE)
    cleaned = _clean(key)
    if cleaned in _GENERIC_PRICE:
        if has_service:
            return Guess(Field.PRICE, Confidence.SURE)
        return Guess(Field.GUEST_POST, Confidence.LIKELY, "Цена без услуги — считаем публикацией.")
    priceish = bool(tokens & {"price", "цена", "cost", "стоимость"})
    if tokens & {"grey", "gray", "casino", "cbd", "sensitive", "серая", "серой"}:
        return Guess(Field.GRAY, Confidence.LIKELY)
    if tokens & {"writing", "написание", "content", "copywriting"} or cleaned == "написание":
        return Guess(
            Field.WRITING, Confidence.LIKELY, "Проверьте валюту: бывает цена Collaborator в евро."
        )
    if tokens & {"announce", "announcement", "анонс"}:
        return Guess(
            Field.ANNOUNCE, Confidence.LIKELY, "Проверьте валюту: бывает цена Collaborator в евро."
        )
    if priceish and tokens & {"gp", "guest", "post", "article", "публикация", "статья"}:
        return Guess(Field.GUEST_POST, Confidence.LIKELY)
    if priceish and tokens & {"li", "insertion", "insert", "niche", "вставка", "вставки"}:
        return Guess(Field.LINK_INSERTION, Confidence.LIKELY)
    if priceish:
        if has_service:
            return Guess(Field.PRICE, Confidence.LIKELY)
        return Guess(Field.GUEST_POST, Confidence.LIKELY, "Цена без услуги — считаем публикацией.")
    if tokens & {"traffic", "трафик", "organic", "органика"}:
        return Guess(
            Field.TRAFFIC,
            Confidence.LIKELY,
            "У продавцов «Traffic» бывает и органический, и общий.",
        )
    if tokens & {"keywords", "ключи", "ключей", "kw"}:
        return Guess(Field.KEYWORDS, Confidence.LIKELY)
    if tokens & {"follow", "dofollow", "nofollow", "rel"}:
        return Guess(Field.LINK_TYPE, Confidence.LIKELY)
    if tokens & {"comment", "comments", "комментарий", "комментарии", "примечание", "заметка"}:
        return Guess(Field.NOTE, Confidence.LIKELY, "Похоже на наши пометки — пойдут в заметки.")
    return Guess(Field.EXTRA, Confidence.UNKNOWN)


def _one_column_per_field(
    columns: Sequence[Column], guesses: dict[str, Guess], tokens: dict[str, set[str]]
) -> dict[str, Guess]:
    """Одно поле — одна колонка: из нескольких кандидатов остаётся лучший.

    Лучший — точная догадка; среди похожих — колонка с «organic» или
    «ahrefs» (трафик Semrush и Ahrefs в одном прайсе), иначе первая.
    Остальные — в «прочие данные».
    """
    by_field: dict[Field, list[Column]] = {}
    for column in columns:
        field = guesses[column.key].field
        if field in SINGLE_FIELDS:
            by_field.setdefault(field, []).append(column)
    for candidates in by_field.values():
        if len(candidates) < 2:
            continue

        def rank(column: Column) -> tuple[int, int, int]:
            sure = guesses[column.key].confidence == Confidence.SURE
            preferred = bool(tokens[column.key] & {"organic", "ahrefs", "ahref"})
            return (0 if sure else 1, 0 if preferred else 1, column.index)

        best = min(candidates, key=rank)
        for column in candidates:
            if column is not best:
                guesses[column.key] = Guess(Field.EXTRA, Confidence.UNKNOWN)
    return guesses
