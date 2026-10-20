"""План выгрузки: атомарные поля, точный порядок образца, один результат для
предпросмотра, буфера, CSV и XLSX. Чтение размещений ничего в них не меняет.

Валюта сумм — два выбора настроек (просьба пользователя 19.10.2026):
«Исходная валюта» — валюта «Итог цены»: как записано в размещении или одна
на все строки; «Пересчитать в» — куда перевести «Итог цену» и цены площадки
(они в евро). Курс — последний ЕЦБ, как в «Экономике» (`latest_rates`).
"""

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet

from apps.placements import export as legacy
from apps.placements.models import Placement, PlacementLink
from apps.sites.domains import normalize_domain
from apps.sites.models import Product
from apps.sites.rates import convert_cents, latest_rates, to_eur_cents
from config import export

SCREEN = "placements.placement"
MAX_COLUMNS = 100
MAX_ROWS = 10000
# Цены площадки «на сегодня» — в евро; при пересчёте переводятся из евро.
EUR_PRICES = ("placement_price", "announce_price", "writing_price")
# Первыми в выборе валюты; остальные — с курсом ЕЦБ, по алфавиту.
MAIN_CURRENCIES = ("EUR", "USD", "GBP")


class DesignError(ValueError):
    """Невалидная настройка; сообщение показывается рядом с формой."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    header: str
    note: str
    column: export.Column

    def as_json(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "header": self.header,
            "note": self.note,
            "kind": self.column.kind.value,
        }


# Метаданные ниже описывают существующие значения, не вычисляют их заново.
LABELS: dict[str, tuple[str, str]] = {
    "target": (
        "Домен площадки",
        "Домен выбранного размещения; заголовок в файле может быть пустым.",
    ),
    "seller": (
        "Продавец размещения",
        "Продавец конкретной заявки. Не продавец текущего рабочего предложения площадки.",
    ),
    "site_note": (
        "Последняя заметка площадки",
        "Сейчас экспортируется последняя заметка. Это не вся история комментариев.",
    ),
    "article_url": (
        "Адрес опубликованной статьи",
        "URL именно выбранного размещения.",
    ),
    "indexed": (
        "Статья в индексе Google",
        "Да / Нет / пусто. Дата проверки — отдельное поле, её можно добавить в свободном профиле.",
    ),
    "status": (
        "Статус размещения",
        "Статус заявки, не статус площадки. «404 Ошибка» в файле не равна автоматически "
        "статусу заявки в системе.",
    ),
    "ordered_at": (
        "Дата отправки заявки",
        "Дата отправки, отдельно от даты публикации.",
    ),
    "published": (
        "Дата публикации",
        "Отдельно от даты отправки заявки. Формат DD.MM.YYYY.",
    ),
    "month": (
        "Месяц публикации",
        "Месяц из даты публикации. В загруженном образце выбирай «Сохранить из файла» для "
        "планового периода.",
    ),
    "comment": (
        "Комментарий к размещению",
        "Комментарий конкретной заявки, не заметка площадки.",
    ),
    "traffic": (
        "Органический трафик — весь мир",
        "Последний общий замер. Не сумма стран, не трафик выбранного региона.",
    ),
    "top_geo": (
        "Страна с наибольшим трафиком",
        "Код страны из общего замера; в образце — нижний регистр us, in и т. п.",
    ),
    "top_geo_traffic": (
        "Трафик страны-лидера",
        "Трафик Top Geo из общего замера. Это не обязательно США.",
    ),
    "us_traffic": (
        "Органический трафик — США",
        "Последний отдельный замер США. Нет замера — пусто, не ноль.",
    ),
    "dr": (
        "DR",
        "Последний DR; свежие значения могут отличаться от исходного файла.",
    ),
    "keywords": (
        "Ключей в органике — весь мир",
        "Последний общий замер числа ключей.",
    ),
    "service": (
        "Формат размещения",
        "В системе публикация / вставка ссылки. Заголовок файла «Тип ссылки» означает Guest "
        "Post / Link Insertion, а не dofollow.",
    ),
    "placement_price": (
        "Текущая рабочая цена публикации, EUR",
        "Сегодняшняя рабочая цена площадки, пересчитанная в EUR. Может относиться к другому "
        "продавцу, чем заявка. Не историческая оплаченная сумма.",
    ),
    "announce_price": (
        "Текущая рабочая цена анонса, EUR",
        "0 — бесплатно; пусто — неизвестно. Не подменять пустое нулём.",
    ),
    "writing_price": (
        "Текущая рабочая цена написания, EUR",
        "Компонент текущего предложения. Не прибавлять к уже записанному фактическому итогу.",
    ),
    "paid": (
        "Фактически оплачено — валюта размещения",
        "Записанная сумма оплаты. Валюту можно вывести отдельной колонкой; итог в EUR "
        "пересчитывается по последнему известному курсу.",
    ),
    "links_allowed": (
        "Разрешено ссылок в статье",
        "Лимит площадки. Не число заполненных ссылок конкретной заявки.",
    ),
    "marks_as_ad": (
        "Площадка маркирует рекламу",
        "Условие площадки. Не подтверждение рекламной пометки на фактической статье.",
    ),
    "declared_topics": (
        "Заявленные особые тематики",
        "Категории площадки, не результат проверки серости в Google.",
    ),
    "languages": (
        "Языки площадки",
        "Список языков, человекочитаемые значения.",
    ),
    "site_type": (
        "Тип площадки",
        "Например, персональный блог.",
    ),
    "collaborator_url": (
        "Карточка в Collaborator",
        "Ссылка на карточку площадки.",
    ),
    "topics": (
        "Тематики площадки",
        "Список тематик через запятую, как в таблице.",
    ),
    "link_type": (
        "Атрибут ссылки размещения",
        "dofollow / nofollow / sponsored / ugc, если значение записано. Не формат "
        "публикации и не условие площадки.",
    ),
}
TEMPLATE_KEYS: tuple[str, ...] = (
    "target",
    "seller",
    "site_note",
    "article_url",
    "indexed",
    "status",
    "ordered_at",
    "published",
    "month",
    "comment",
    "links.1.anchor",
    "links.1.url",
    "links.2.anchor",
    "links.2.url",
    "example.clideo",
    "traffic",
    "top_geo",
    "top_geo_traffic",
    "us_traffic",
    "dr",
    "keywords",
    "service",
    "placement_price",
    "announce_price",
    "writing_price",
    "paid",
    "links_allowed",
    "marks_as_ad",
    "declared_topics",
    "languages",
    "site_type",
    "collaborator_url",
    "topics",
    "link_type",
)


def fields() -> dict[str, Field]:
    result: dict[str, Field] = {}
    for key, column, _ in legacy._SPEC:
        if column is None:
            continue
        label, note = LABELS.get(key, (column.title, "Значение выбранного размещения."))
        result[key] = Field(key, label, column.title, note, column)
    result["record_id"] = Field(
        "record_id",
        "ID размещения",
        "ID размещения",
        "Устойчивый идентификатор конкретной заявки.",
        export.Column("ID размещения", export.Kind.INT),
    )
    # Пары фиксированы: третья ссылка не добавляет колонок сохранённому профилю.
    for slot in range(1, 11):
        for part, title in (("anchor", "Анкор"), ("url", "Ссылка")):
            key = f"links.{slot}.{part}"
            result[key] = Field(
                key,
                f"{title} ссылки №{slot}",
                f"{title}{slot}",
                "Фактическая ссылка с этим номером. Пропуск номера остаётся пустым.",
                export.Column(f"{title}{slot}", width=35),
            )
    for pk, name in Product.objects.order_by("pk").values_list("pk", "name"):
        key = f"example.{pk}"
        title = f"Пример статьи на {name}"
        result[key] = Field(
            key,
            title,
            title,
            "Последняя опубликованная статья этого продукта.",
            export.Column(title, width=45),
        )
    result["month_ordered"] = Field(
        "month_ordered",
        "Месяц отправки заявки",
        "Месяц заявки",
        "Месяц из даты отправки заявки.",
        export.Column("Месяц заявки", export.Kind.MONTH),
    )
    return result


def initial(registry: dict[str, Field]) -> dict[str, Any]:
    clideo = next(
        (f.key for f in registry.values() if f.header.casefold() == "пример статьи на clideo"),
        "blank",
    )
    columns = []
    for index, key in enumerate(TEMPLATE_KEYS):
        key = clideo if key == "example.clideo" else key
        item = registry.get(key)
        columns.append(
            {
                "id": f"c{index}",
                "index": index,
                "header": ""
                if index == 0
                else "Дата отправки"
                if key == "ordered_at"
                else item.header
                if item
                else "Пример статьи на Clideo",
                "source": key,
                "enabled": True,
                "constant": "",
            }
        )
    return {
        "version": 1,
        "mode": "custom",
        "order": "current",
        "columns": columns,
        "delimiter": ";",
        # Как в рабочей Google-таблице (формат Financial): 206.14, 7.50 — без знака валюты.
        "decimal": ".",
        # Пусто — «как в размещении» и «не пересчитывать».
        "currency_from": "",
        "currency_to": "",
    }


def validate(raw: object, registry: dict[str, Field]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise DesignError("Неизвестная версия шаблона.")
    columns = raw.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise DesignError(f"Нужно от 1 до {MAX_COLUMNS} колонок.")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in columns:
        if not isinstance(value, dict):
            raise DesignError("Не удалось прочитать колонку.")
        key, title, source = value.get("id"), value.get("header"), value.get("source")
        if (
            not isinstance(key, str)
            or not key
            or key in seen
            or not isinstance(title, str)
            or len(title) > 200
            or not isinstance(source, str)
            or source not in {*registry, "blank", "constant", "original", "unmapped"}
        ):
            raise DesignError("Колонка недоступна или имеет неверные настройки.")
        seen.add(key)
        index, enabled, constant = (
            value.get("index"),
            value.get("enabled"),
            value.get("constant", ""),
        )
        if (
            (index is not None and (type(index) is not int or not 0 <= index < MAX_COLUMNS))
            or not isinstance(enabled, bool)
            or not isinstance(constant, str)
            or len(constant) > 5000
        ):
            raise DesignError("Неверные настройки значения колонки.")
        clean.append(
            {
                "id": key,
                "header": title,
                "source": source,
                "index": index,
                "enabled": enabled,
                "constant": constant,
            }
        )
    mode, order = raw.get("mode"), raw.get("order")
    if mode not in ("custom", "template") or order not in ("current", "file"):
        raise DesignError("Выберите режим колонок и порядок строк.")
    delimiter, decimal = raw.get("delimiter", ";"), raw.get("decimal", ".")
    if delimiter not in (",", ";", "\t") or decimal not in (".", ","):
        raise DesignError("Неверный формат CSV.")
    if not any(c["enabled"] for c in clean) and mode == "custom":
        raise DesignError("Включите хотя бы одну колонку.")
    # У шаблонов до выбора валюты ключей нет — как было: без пересчёта.
    source, target = raw.get("currency_from", ""), raw.get("currency_to", "")
    known = {"", *currencies()}
    if source not in known or target not in known:
        raise DesignError("Выберите валюту из списка.")
    return {
        "version": 1,
        "mode": mode,
        "order": order,
        "columns": clean,
        "delimiter": delimiter,
        "decimal": decimal,
        "currency_from": source,
        "currency_to": target,
    }


def currencies() -> list[str]:
    """Валюты выбора: евро, доллар, фунт — первыми, остальные с курсом ЕЦБ."""
    known = set(latest_rates())
    first = [code for code in MAIN_CURRENCIES if code in known]
    return first + sorted(known - set(first))


def convert(records: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Суммы строк — в валюте из настроек; курса нет — ячейка пустая.

    «Итог цена» — из исходной валюты (своя у каждого размещения, если её не
    выбрали одну на все), «Итог цена, EUR» — от неё же. Цены площадки — из
    евро. «Валюта» — во что пересчитали.
    """
    source, target = config["currency_from"], config["currency_to"]
    if not source and not target:
        return
    rates = latest_rates()
    for record in records:
        values = record["values"]
        paid = values.get("paid")
        if isinstance(paid, int):
            paid_currency = source or str(values.get("currency") or "EUR")
            values["paid_eur"] = to_eur_cents(paid, paid_currency, rates)
            values["currency"] = target or paid_currency
            if target:
                values["paid"] = convert_cents(paid, paid_currency, target, rates)
        if not target:
            continue
        for key in EUR_PRICES:
            cents = values.get(key)
            if isinstance(cents, int):
                values[key] = convert_cents(cents, "EUR", target, rates)


def _header(spec: dict[str, Any], config: dict[str, Any], registry: dict[str, Field]) -> str:
    """Заголовок колонки: у пересчитанной цены площадки «, EUR» стандартного
    заголовка меняется на выбранную валюту; свой заголовок — как вписан."""
    header: str = spec["header"]
    target = config["currency_to"]
    source = spec["source"]
    # Снятая галочка в образце — значение из файла, его не пересчитывали.
    if target and spec["enabled"] and source in EUR_PRICES and header == registry[source].header:
        return header.replace("EUR", target)
    return header


def dataset(queryset: QuerySet[Placement], registry: dict[str, Field]) -> list[dict[str, Any]]:
    metadata = list(
        queryset.values(
            "pk",
            "site_id",
            "product_id",
            "site__domain",
            "seller__name",
            "article_url",
            "ordered_at",
        )
    )
    if len(metadata) > MAX_ROWS:
        raise DesignError(f"Выберите не более {MAX_ROWS} размещений.")
    sheet = legacy.sheets(queryset, {c.key for c in legacy.choices()})[0]
    titles = [c.title for c in sheet.columns]
    links: dict[int, dict[str, export.Value]] = defaultdict(dict)
    for pk, slot, anchor, url in PlacementLink.objects.filter(
        placement_id__in=[m["pk"] for m in metadata]
    ).values_list("placement_id", "link_index", "anchor", "target_url"):
        if slot is not None:
            links[pk][f"links.{slot}.anchor"] = anchor
            links[pk][f"links.{slot}.url"] = url
    articles = legacy._published_articles(sorted({m["site_id"] for m in metadata}))
    result: list[dict[str, Any]] = []
    for meta, cells in zip(metadata, sheet.rows, strict=True):
        by_title = dict(zip(titles, cells, strict=True))
        values: dict[str, export.Value] = {}
        for key, item in registry.items():
            if key.startswith("links."):
                values[key] = links[meta["pk"]].get(key)
            elif key.startswith("example."):
                values[key] = legacy._other_article(
                    articles, meta["site_id"], int(key.split(".")[1]), meta["pk"]
                )
            else:
                values[key] = by_title.get(item.column.title)
        values["record_id"] = meta["pk"]
        values["month_ordered"] = meta["ordered_at"]
        values["service"] = {"публикация": "Guest Post", "вставка ссылки": "Link Insertion"}.get(
            str(values.get("service")), values.get("service")
        )
        result.append({**meta, "values": values})
    return result


def suggest(headers: list[str], registry: dict[str, Field]) -> dict[str, Any]:
    aliases = {f.header.casefold().strip(): f.key for f in registry.values()}
    aliases.update({f.label.casefold().strip(): f.key for f in registry.values()})
    aliases.update(
        {
            "": "target",
            "домен": "target",
            "сайт": "target",
            "площадка": "target",
            "дата отправки": "ordered_at",
            "анкор 1": "links.1.anchor",
            "ссылка 1": "links.1.url",
            "анкор 2": "links.2.anchor",
            "ссылка 2": "links.2.url",
        }
    )
    config = initial(registry)
    config.update(mode="template", order="file")
    config["columns"] = [
        {
            "id": f"c{i}",
            "index": i,
            "header": header,
            "source": "original"
            if header.casefold().strip() == "месяц"
            else aliases.get(header.casefold().strip(), "unmapped"),
            "enabled": True,
            "constant": "",
        }
        for i, header in enumerate(headers)
    ]
    # Единственный пустой A обычно содержит домены; остальные пустые поля не угадываем.
    for col in config["columns"][1:]:
        if not col["header"]:
            col["source"] = "original"
    return config


@dataclass
class Plan:
    sheet: export.Sheet
    issues: list[dict[str, Any]]
    row_numbers: list[int]
    digest: str


def build_plan(
    records: list[dict[str, Any]],
    config: dict[str, Any],
    registry: dict[str, Field],
    template: dict[str, Any] | None = None,
    decisions: dict[str, Any] | None = None,
) -> Plan:
    decisions = decisions or {}
    if any(
        not isinstance(d, dict)
        or d.get("action") not in ("keep", "exclude", "match")
        or (d.get("action") == "match" and type(d.get("id")) is not int)
        for d in decisions.values()
    ):
        raise DesignError("Неверное решение для строки образца.")
    if template is not None and any(
        c["index"] is not None and c["index"] >= len(template["headers"]) for c in config["columns"]
    ):
        raise DesignError("Колонка выходит за границы образца.")
    if config["mode"] == "template" and template is None:
        raise DesignError("Для фиксированного порядка загрузите файл-образец.")
    specs = (
        config["columns"]
        if config["mode"] == "template"
        else [c for c in config["columns"] if c["enabled"]]
    )
    if config["mode"] == "template":
        if len(specs) != len(template["headers"]):  # type: ignore[index]
            raise DesignError("Количество колонок не совпадает с образцом.")
        specs = sorted(specs, key=lambda c: c["index"] if c["index"] is not None else -1)
        if [c["index"] for c in specs] != list(range(len(specs))):
            raise DesignError("Колонки образца должны сохранять исходные позиции.")
    columns = [
        export.Column(
            _header(c, config, registry),
            registry[c["source"]].column.kind
            if c["source"] in registry and c["enabled"]
            else export.Kind.TEXT,
            registry[c["source"]].column.width if c["source"] in registry else 24,
        )
        for c in specs
    ]
    issues: list[dict[str, Any]] = []
    order: list[tuple[int, dict[str, Any] | None, list[str] | None, bool]] = []
    by_id = {r["pk"]: r for r in records}
    if config["order"] == "file":
        if template is None:
            raise DesignError("Загрузите образец, чтобы сохранить его порядок строк.")
        domain_col = next((c["index"] for c in config["columns"] if c["source"] == "target"), None)
        if domain_col is None:
            raise DesignError("Сопоставьте колонку домена с полем «Домен площадки».")

        def original(cells: list[str], key: str) -> str:
            col = next((c for c in config["columns"] if c["source"] == key), None)
            return cells[col["index"]] if col is not None and col["index"] is not None else ""

        domains: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            domains[normalize_domain(r["site__domain"])].append(r)
        repeats = Counter(
            normalize_domain(r["cells"][domain_col])
            for r in template["rows"]
            if r["cells"][domain_col].strip()
        )
        for row in template["rows"]:
            line, cells = row["line"], row["cells"]
            decision = decisions.get(str(line), {})
            if decision.get("action") == "exclude":
                continue
            if decision.get("action") == "keep" or not any(cells):
                order.append((line, None, cells, True))
                continue
            domain = normalize_domain(cells[domain_col])
            candidates = domains.get(domain, [])
            picked = None
            reason = "Не найдено в текущем отборе"
            if decision.get("action") == "match":
                picked = by_id.get(decision.get("id"))
                if picked is None or normalize_domain(picked["site__domain"]) != domain:
                    raise DesignError(
                        f"Строка {line}: размещение не входит в отбор или другой домен."
                    )
            else:
                record_id, url, seller = (
                    original(cells, k) for k in ("record_id", "article_url", "seller")
                )
                exact = [
                    r
                    for r in candidates
                    if (record_id and str(r["pk"]) == record_id)
                    or (url and r["article_url"] == url)
                ]
                if len(exact) == 1:
                    picked = exact[0]
                elif len(candidates) == 1 and repeats[domain] == 1:
                    candidate = candidates[0]
                    if (url and url != candidate["article_url"]) or (
                        seller and seller != candidate["seller__name"]
                    ):
                        reason = "URL статьи или продавец отличаются"
                    else:
                        picked = candidate
                elif candidates:
                    reason = "Повтор домена или несколько размещений"
            if picked is None:
                issues.append(
                    {
                        "line": line,
                        "domain": domain,
                        "reason": reason,
                        "candidates": [
                            {"id": r["pk"], "seller": r["seller__name"], "url": r["article_url"]}
                            for r in candidates
                        ],
                    }
                )
            order.append((line, picked, cells, False))
    else:
        order = [(i, r, None, False) for i, r in enumerate(records, 1)]
    table: list[list[export.Value]] = []
    lines: list[int] = []
    for line, record, cells, keep in order:
        values: list[export.Value] = []
        for spec in specs:
            source, index = spec["source"], spec["index"]
            previous = cells[index] if cells is not None and index is not None else None
            preserving = source == "original" or (
                config["mode"] == "template" and not spec["enabled"]
            )
            if keep or preserving:
                if cells is None:
                    raise DesignError("«Сохранить из файла» доступно только с порядком по образцу.")
                value: export.Value = previous
            elif source == "constant":
                value = spec["constant"]
            elif source == "blank":
                value = None
            elif source == "unmapped":
                raise DesignError(f"Выберите источник для колонки «{spec['header']}».")
            else:
                value = record["values"].get(source) if record is not None else previous
            values.append(value)
        table.append(values)
        lines.append(line)
    digest = hashlib.sha256(
        json.dumps(
            [config, table, issues, lines], ensure_ascii=False, default=str, sort_keys=True
        ).encode()
    ).hexdigest()
    return Plan(export.Sheet("Размещения", columns, table), issues, lines, digest)


def display(column: export.Column, value: export.Value, config: dict[str, Any]) -> str:
    text = export._csv_value(column, value)
    if config["decimal"] == "." and column.kind in (export.Kind.MONEY, export.Kind.NUMBER):
        text = text.replace(",", ".")
    return text


def csv_data(sheet: export.Sheet, config: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=config["delimiter"], lineterminator="\r\n")
    # Заголовок тоже внешние данные; защиту формул применяем к нему отдельно.
    writer.writerow([export._csv_value(export.Column(""), c.title) for c in sheet.columns])
    writer.writerows(
        [
            [display(c, v, config) for c, v in zip(sheet.columns, row, strict=True)]
            for row in sheet.rows
        ]
    )
    return buffer.getvalue().encode("utf-8-sig")
