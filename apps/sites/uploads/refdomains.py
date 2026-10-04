"""Выгрузка Ahrefs «Referring domains» → кто ссылается на продукт (E1-09, ADR-051).

Файл — UTF-16 с табуляцией, как его отдаёт Export на странице Referring domains
в Site Explorer по домену продукта (чтение — `files`); разметка встроена.
Нужны колонки Domain и First seen; Lost — если выгрузка с пропавшими.

Домены хранятся отдельно от площадок (`product_ref_domains`): google.com и
тысячи случайных доменов в «Площадки» не попадают. Совпадение с площадкой —
точное, после нормализации домена (без `www.`).

Запись:
- домен из файла — строка продукта; есть — обновляется: первое появление,
  дата Lost, «есть в выгрузке от»; пометка «нет в выгрузках» снимается;
- выгрузка — самая свежая у продукта: домены, которых в ней нет, получают
  «нет в выгрузках с ‹дата выгрузки›» — ссылка, похоже, пропала. Старая
  выгрузка, загруженная после новой, только добавляет: пропажу по ней не
  отмечаем, даты назад не двигаем;
- строки не удаляются.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db import connection
from django.db.models import Max
from django.utils import timezone

from apps.sites.domains import normalize_domain
from apps.sites.models import Product, ProductRefDomain, Site, Upload
from apps.sites.uploads.files import Table, is_blank, show
from apps.sites.uploads.plan import LIST_LIMIT

DOMAIN = "Domain"
FIRST_SEEN = "First seen"
LOST = "Lost"
REQUIRED = (DOMAIN, FIRST_SEEN)
BATCH = 1000
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y")


@dataclass(frozen=True)
class RefDomain:
    line: int
    domain: str
    first_seen_at: dt.datetime | None
    lost_at: dt.datetime | None


@dataclass(frozen=True)
class Problem:
    line: int
    message: str


@dataclass
class Parsed:
    domains: list[RefDomain]
    errors: list[Problem] = field(default_factory=list)
    duplicates: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)


def looks_like_refdomains(cells: Sequence[str]) -> bool:
    return DOMAIN in cells and FIRST_SEEN in cells


def missing_columns(table: Table) -> list[str]:
    headers = {column.header for column in table.columns}
    return [name for name in REQUIRED if name not in headers]


def parse(table: Table) -> Parsed:
    """Строки файла → домены. Битая строка — в ошибки, загрузку не останавливает."""
    index = {column.header: column.index for column in table.columns}
    parsed = Parsed(domains=[])
    lines: dict[str, list[int]] = {}
    for row in table.rows:
        try:
            ref = _ref(row.line, {header: row.get(i) for header, i in index.items()})
        except ValueError as error:
            parsed.errors.append(Problem(row.line, str(error)))
            continue
        lines.setdefault(ref.domain, []).append(row.line)
        if len(lines[ref.domain]) == 1:
            parsed.domains.append(ref)
    parsed.duplicates = [(d, tuple(ls)) for d, ls in lines.items() if len(ls) > 1]
    return parsed


def _ref(line: int, cells: dict[str, object]) -> RefDomain:
    raw = show(cells.get(DOMAIN))
    if not raw:
        raise ValueError("нет домена")
    try:
        domain = normalize_domain(raw)
    except ValueError:
        raise ValueError(f"не домен: {raw!r}") from None
    if "." not in domain:
        raise ValueError(f"не домен: {raw!r}")
    return RefDomain(line, domain, _moment(cells.get(FIRST_SEEN)), _moment(cells.get(LOST)))


def _moment(value: object) -> dt.datetime | None:
    """Время из выгрузки Ahrefs: `2023-08-10 15:51:35` — по UTC."""
    if is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        return value if timezone.is_aware(value) else value.replace(tzinfo=dt.UTC)
    text = show(value)
    for pattern in _FORMATS:
        try:
            return dt.datetime.strptime(text, pattern).replace(tzinfo=dt.UTC)
        except ValueError:
            continue
    raise ValueError(f"не дата: {text!r}")


def linking(domains: Sequence[str]) -> list[dict[str, Any]]:
    """Сколько доменов файла уже ссылаются на каждый продукт — для сводки прайса и каталога.

    Только те, что ссылаются сейчас: пропавшая ссылка площадку в «Площадках» не прячет.
    """
    found: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    unique = sorted(set(domains))
    for start in range(0, len(unique), BATCH * 5):
        rows = ProductRefDomain.objects.filter(
            domain__in=unique[start : start + BATCH * 5],
            lost_at__isnull=True,
            missing_since__isnull=True,
        ).values_list("product_id", "product__name", "domain")
        for product_id, name, domain in rows:
            found.setdefault(product_id, []).append(domain)
            names[product_id] = name
    return [
        {
            "product_id": product_id,
            "product": names[product_id],
            "total": len(found[product_id]),
            "domains": sorted(found[product_id])[:LIST_LIMIT],
        }
        for product_id in sorted(found)
    ]


# --- План ---


@dataclass
class Plan:
    parsed: Parsed
    product: Product
    day: dt.date  # дата выгрузки
    existing: dict[str, ProductRefDomain]  # домен → строка продукта
    newest: bool  # самая свежая выгрузка у продукта: по ней отмечается пропажа
    sites: set[str]  # домены файла, которые есть среди площадок

    @property
    def missing(self) -> list[ProductRefDomain]:
        """Ссылались, а в этой выгрузке их нет: получат «нет в выгрузках с».

        Уже пропавшие — по дате Lost или прошлой выгрузке — не в счёт.
        """
        if not self.newest:
            return []
        present = {ref.domain for ref in self.parsed.domains}
        return [
            row for domain, row in self.existing.items() if domain not in present and row.is_linking
        ]

    def summary(self, *, rows: int, blank_rows: int) -> dict[str, Any]:
        domains = self.parsed.domains
        known = [ref for ref in domains if ref.domain in self.existing]
        returned = [
            ref
            for ref in known
            if self.existing[ref.domain].missing_since is not None
            or (self.existing[ref.domain].lost_at is not None and ref.lost_at is None)
        ]
        missing = sorted(row.domain for row in self.missing)
        in_sites = sorted(ref.domain for ref in domains if ref.domain in self.sites)
        return {
            "rows": rows,
            "blank_rows": blank_rows,
            "product": self.product.name,
            "domains": len(domains),
            "new": len(domains) - len(known),
            "known": len(known),
            "returned": len(returned),
            "lost_in_file": sum(1 for ref in domains if ref.lost_at is not None),
            "newest": self.newest,
            **_capped("missing", [{"domain": d} for d in missing]),
            **_capped("in_sites", [{"domain": d} for d in in_sites]),
            **_capped(
                "errors", [{"line": e.line, "message": e.message} for e in self.parsed.errors]
            ),
            **_capped(
                "duplicates", [{"domain": d, "lines": list(ls)} for d, ls in self.parsed.duplicates]
            ),
        }


def build_plan(parsed: Parsed, upload: Upload) -> Plan:
    product = upload.get_product()
    rows = ProductRefDomain.objects.filter(product=product)
    existing = {row.domain: row for row in rows.order_by("pk")}
    latest = rows.aggregate(latest=Max("seen_on"))["latest"]
    domains = [ref.domain for ref in parsed.domains]
    sites: set[str] = set()
    for start in range(0, len(domains), BATCH):
        chunk = domains[start : start + BATCH]
        sites.update(Site.objects.filter(domain__in=chunk).values_list("domain", flat=True))
    newest = latest is None or upload.prices_date >= latest
    return Plan(parsed, product, upload.prices_date, existing, newest, sites)


# --- Запись ---


def write(plan: Plan, upload: Upload) -> dict[str, Any]:
    """Строки продукта: новые создаются, известные обновляются, пропавшие отмечаются.

    Вызывается внутри транзакции и блокировки загрузки (`service.write`).
    """
    now = timezone.now()
    new: list[ProductRefDomain] = []
    changed: list[ProductRefDomain] = []
    for ref in plan.parsed.domains:
        row = plan.existing.get(ref.domain)
        if row is None:
            new.append(
                ProductRefDomain(
                    product=plan.product,
                    domain=ref.domain,
                    first_seen_at=ref.first_seen_at,
                    lost_at=ref.lost_at,
                    seen_on=plan.day,
                    upload=upload,
                )
            )
            continue
        if _apply(row, ref, plan.day, upload, now):
            changed.append(row)
    missing = plan.missing
    for row in missing:
        row.missing_since = plan.day
        row.updated_at = now
    ProductRefDomain.objects.bulk_create(new, batch_size=BATCH)
    fields = ["first_seen_at", "lost_at", "seen_on", "missing_since", "upload", "updated_at"]
    ProductRefDomain.objects.bulk_update([*changed, *missing], fields, batch_size=BATCH)
    # Статистика — сразу: без неё Postgres планирует фильтр «Ссылаются на нас» так,
    # будто таблица пуста, и «Площадки» после первой выгрузки открываются в разы
    # дольше, пока не придёт автоанализ (замер E1-09).
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE product_ref_domains")
    return {
        "counts": {
            "created": len(new),
            "updated": len(changed),
            "unchanged": len(plan.parsed.domains) - len(new) - len(changed),
            "missing": len(missing),
        },
        "domains": len(plan.parsed.domains),
    }


def _apply(
    row: ProductRefDomain, ref: RefDomain, day: dt.date, upload: Upload, now: dt.datetime
) -> bool:
    """Обновить строку из выгрузки. Старая выгрузка даты назад не двигает."""
    fresh = day >= row.seen_on
    wanted: dict[str, Any] = {}
    if ref.first_seen_at is not None and row.first_seen_at != ref.first_seen_at:
        wanted["first_seen_at"] = ref.first_seen_at
    if fresh:
        wanted.update({"lost_at": ref.lost_at, "seen_on": day, "missing_since": None})
        wanted["upload_id"] = upload.pk
    changed = False
    for name, value in wanted.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    if changed:
        row.updated_at = now
    return changed


def _capped(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {name: rows[:LIST_LIMIT], f"{name}_total": len(rows)}
