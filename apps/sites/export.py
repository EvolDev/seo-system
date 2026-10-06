"""Выгрузка «Площадок» в Excel и CSV (E1-06, ADR-053).

Строки — ровно те, что отобраны на экране (фильтры, поиск, регион,
сортировка), все страницы сразу; их отбирает список админки, сюда приходит
готовая выборка `v_product_site_latest`. Колонки — как на экране, но
значения разложены по ячейкам: у цены — сумма, валюта, в евро, услуга и
продавец отдельно, чтобы в Excel их можно было сортировать и складывать.
"""

from collections.abc import Iterator, Sequence, Set
from typing import Any

from django.db import models

from apps.sites.models import AuditVerdict, PlacementType, SiteStatus
from config.export import Choice, Column, Kind, Sheet, Value, narrow

STATUS = dict(SiteStatus.choices)
SERVICE = dict(PlacementType.choices)
VERDICT = dict(AuditVerdict.choices)

SITE = "Площадка"
METRICS = "Метрики"
PRICE = "Рабочая цена"
OFFERS = "Другие предложения"
WORK = "Работа и заметки"

# Колонки региона — аннотации, которые «Площадки» добавляют к выборке; в окне
# выгрузки — одна галочка на три колонки.
REGION = "region"
REGION_TRAFFIC = "region_traffic"
REGION_KEYWORDS = "region_keywords"
REGION_AT = "region_at"
_REGION_FIELDS = (REGION_TRAFFIC, REGION_KEYWORDS, REGION_AT)

# Поле выборки → колонка файла и раздел окна выгрузки. Порядок — как на экране
# «Площадки», потом то, что на экране видно только в подсказках и карточке.
# Ключ галочки — имя поля, у колонок региона — общий `REGION`.
_BEFORE_REGION: tuple[tuple[str, Column, str], ...] = (
    ("domain", Column("Домен", width=28), SITE),
    ("status", Column("Статус", width=18), SITE),
    ("comment", Column("Комментарий", width=30), SITE),
    ("dr", Column("DR", Kind.INT, 6), METRICS),
    ("organic_traffic", Column("Трафик", Kind.INT, 12), METRICS),
    ("total_keywords", Column("Ключей", Kind.INT, 10), METRICS),
    ("top_geo", Column("Топ регион", width=8), METRICS),
    ("top_geo_traffic", Column("Трафик топ региона", Kind.INT, 12), METRICS),
)
_AFTER_REGION: tuple[tuple[str, Column, str], ...] = (
    ("metrics_seller", Column("Метрики со слов продавца", width=18), METRICS),
    ("metrics_at", Column("Дата метрик", Kind.DATE, 12), METRICS),
    ("language", Column("Язык", width=6), SITE),
    ("placement_cents", Column("Цена", Kind.MONEY, 11), PRICE),
    ("price_currency", Column("Валюта", width=7), PRICE),
    ("placement_eur_cents", Column("Цена, EUR", Kind.MONEY, 11), PRICE),
    ("price_type", Column("Услуга", width=14), PRICE),
    ("price_seller", Column("Продавец", width=18), PRICE),
    ("prices_at", Column("Дата цены", Kind.DATE, 12), PRICE),
    ("announce_cents", Column("Анонс", Kind.MONEY, 10), PRICE),
    ("writing_cents", Column("Написание", Kind.MONEY, 11), PRICE),
    ("we_write", Column("Пишем мы", width=9), PRICE),
    ("expected_spend_cents", Column("Плановые расходы, EUR", Kind.MONEY, 12), PRICE),
    ("cheaper_seller", Column("Дешевле у продавца", width=18), OFFERS),
    ("cheaper_cents", Column("Дешевле: цена", Kind.MONEY, 11), OFFERS),
    ("cheaper_currency", Column("Дешевле: валюта", width=7), OFFERS),
    ("cheaper_eur_cents", Column("Дешевле, EUR", Kind.MONEY, 11), OFFERS),
    ("new_price_cents", Column("Новая цена того же продавца", Kind.MONEY, 12), OFFERS),
    ("new_price_currency", Column("Валюта новой цены", width=7), OFFERS),
    ("last_verdict", Column("Аудит", width=12), WORK),
    ("last_score", Column("Оценка", Kind.INT, 7), WORK),
    ("placements_published", Column("Опубликовано", Kind.INT, 8), WORK),
    ("other_products", Column("Другие продукты", width=16), WORK),
    ("topics", Column("Тематики", width=30), SITE),
    ("declared_topics", Column("Особые тематики", width=24), SITE),
    ("link_type", Column("Тип ссылки", width=10), SITE),
    ("links_allowed", Column("Ссылок в статье", Kind.INT, 8), SITE),
    ("marks_as_ad", Column("Пометка «реклама»", width=9), SITE),
    ("gray_ratio", Column("Доля серых, %", Kind.NUMBER, 8), METRICS),
    ("notes_count", Column("Заметок", Kind.INT, 8), WORK),
    ("last_note", Column("Последняя заметка", width=40), WORK),
    ("last_note_at", Column("Дата заметки", Kind.DATE, 12), WORK),
    ("site__collaborator_url", Column("Карточка на Collaborator", width=30), SITE),
)

# Метрики «со слов продавца» — только когда замер не доверенный: как значок у DR.
_HIDDEN = ("metrics_trusted",)


def choices(region: str | None) -> list[Choice]:
    """Галочки окна выгрузки по разделам; колонки региона — если он выбран."""
    found: dict[str, list[Choice]] = {}
    for field, column, group in _fields(region):
        key = _key(field)
        title = column.title
        if key == REGION:
            if field != REGION_TRAFFIC:
                continue  # три колонки — одна галочка
            title = f"Регион {(region or '').upper()}: трафик, ключи, замер"
        found.setdefault(group, []).append(Choice(key, title, group))
    return [choice for group in found.values() for choice in group]


def sheet(queryset: models.QuerySet[Any], region: str | None, wanted: Set[str]) -> Sheet:
    """Лист «Площадки»: выбранные колонки; региона — если он выбран, как на экране."""
    fields = _fields(region)
    names = [field for field, _, _ in fields]
    rows = queryset.values_list(*names, *_HIDDEN)
    layout = [(_key(field), column) for field, column, _ in fields]
    columns, values = narrow(layout, _rows(rows.iterator(chunk_size=2000), names), wanted)
    return Sheet("Площадки", columns, values)


def _fields(region: str | None) -> list[tuple[str, Column, str]]:
    fields = list(_BEFORE_REGION)
    if region is not None:
        label = region.upper()
        fields += [
            (REGION_TRAFFIC, Column(f"Трафик {label}", Kind.INT, 12), METRICS),
            (REGION_KEYWORDS, Column(f"Ключи {label}", Kind.INT, 10), METRICS),
            (REGION_AT, Column(f"Замер {label}", Kind.DATE, 12), METRICS),
        ]
    return fields + list(_AFTER_REGION)


def _key(field: str) -> str:
    return REGION if field in _REGION_FIELDS else field


def _rows(rows: Iterator[Sequence[Any]], names: list[str]) -> Iterator[list[Value]]:
    at = {name: index for index, name in enumerate(names)}
    status, service, verdict = at["status"], at["price_type"], at["last_verdict"]
    top_geo, seller = at["top_geo"], at["metrics_seller"]
    trusted = len(names)
    for raw in rows:
        row: list[Value] = list(raw[:trusted])
        row[status] = _label(STATUS, row[status])
        row[service] = _label(SERVICE, row[service])
        row[verdict] = _label(VERDICT, row[verdict])
        geo = row[top_geo]
        if isinstance(geo, str):
            row[top_geo] = geo.upper()
        if raw[trusted] is not False:
            row[seller] = None
        yield row


def _label(labels: dict[str, str], value: Value) -> Value:
    """Подпись значения из списка выбора: «placed» → «Размещались»."""
    return labels.get(value, value) if isinstance(value, str) else value
