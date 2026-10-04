"""Вкладка «Счета» таблицы линкбилдинга: счета прямых продавцов (E1-14, ADR-055).

Строка — площадка в счёте: площадка, ссылка на статью, цена, цена за пачку,
ссылка на оплату, вебмастер или агентство (маппинг §1.9). Строки с одной
ссылкой на оплату — один счёт: одно размещение или пачка. По ссылке на оплату
счёт находится при повторном импорте: он не дублируется, а расходящийся с
базой — в отчёт и не трогается (ADR-033).

- Размещение — Convertio на этой площадке, по ссылке на статью: то же правило,
  что у размещений таблицы. Не нашлось — счёт не записан, строка в отчёте.
- Сумма — «Цена за пачку», а без неё — сумма «Цены» строк, в евро. Цены строк
  дают сумму пачки — это доли; не дают или пусто — поровну, как «Средняя цена
  за сайт» листа.
- Продавец — продавец размещений; у них его нет — по «Вебмастеру\\агентству»:
  имя или контакт известного продавца, иначе новый продавец с этим именем.
  Почта из этой колонки дописывается в контакты продавца.
- Дат в листе нет: счёт «Оплачен» (лист ведут по оплаченным), выставлен — днём
  публикации первого размещения, без неё — датой выгрузки.
"""

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass

from django.utils import timezone

from apps.placements import invoices
from apps.placements.matching import match_placement
from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement
from apps.sites.domains import normalize_domain
from apps.sites.importing import values
from apps.sites.importing.report import Outcome, Report, Section
from apps.sites.importing.workbook import Sheet
from apps.sites.models import Product, Seller

INVOICES_SHEET = "Счета"
SITE = "Площадка"
ARTICLE_URL = "Ссылка на статью"
PRICE = "Цена EUR"
BATCH_PRICE = "Цена за пачку EUR"
PAY_URL = "Ссылка на оплату"
CONTACT = "Вебмастер\\агентство"
INVOICES_REQUIRED = (SITE, ARTICLE_URL, PRICE, PAY_URL)
CURRENCY = "EUR"
COMMENT = "Из листа «Счета» таблицы: дат выставления и оплаты в листе нет."
# «---» и подобное в ячейке — пусто.
_DASHES = re.compile(r"^[-—–\s]*$")


@dataclass(frozen=True)
class InvoiceRow:
    row: int
    domain: str
    article_url: str | None
    price_cents: int | None
    batch_cents: int | None
    pay_url: str
    contact: str | None


def parse_invoices(sheet: Sheet, report: Report) -> list[InvoiceRow]:
    """Строки вкладки «Счета»; что не разобралось — в отчёт, строка не записывается."""
    result = []
    for row in sheet.rows:
        where = f"строка {row.number}"
        site = values.text(row.get(SITE))
        pay_url = values.text(row.get(PAY_URL))
        if site is None or pay_url is None:
            missing = "площадки" if site is None else "ссылки на оплату"
            report.issue(Section.INVOICES, f"{where}: нет {missing} — не записана")
            continue
        try:
            domain = normalize_domain(site)
            price = _cents(row.get(PRICE))
            batch = _cents(row.get(BATCH_PRICE))
        except ValueError as error:
            report.issue(Section.INVOICES, f"{where}: {error} — не записана")
            continue
        contact = values.text(row.get(CONTACT))
        result.append(
            InvoiceRow(
                row=row.number,
                domain=domain,
                article_url=values.text(row.get(ARTICLE_URL)),
                price_cents=price,
                batch_cents=batch,
                pay_url=pay_url,
                contact=None if contact is None or _DASHES.match(contact) else contact,
            )
        )
    return result


def _cents(value: object) -> int | None:
    """Сумма ячейки; «181,00» из таблицы с русскими настройками — тоже сумма."""
    if isinstance(value, str):
        if _DASHES.match(value):
            return None
        if "," in value and "." not in value:
            value = value.replace(",", ".")
    return values.parse_cents(value)


def import_invoices(
    rows: list[InvoiceRow], *, product: Product, as_of: dt.date, report: Report
) -> None:
    """Счета по строкам вкладки; «Заплачено» размещений — из них (`sync_paid`)."""
    groups: dict[str, list[InvoiceRow]] = defaultdict(list)
    for row in rows:
        groups[row.pay_url].append(row)
    pools: dict[str, list[Placement]] = defaultdict(list)
    for placement in Placement.objects.filter(
        product=product, site__domain__in={row.domain for row in rows}
    ).select_related("site"):
        pools[placement.site.domain].append(placement)
    existing = {
        invoice.pay_url: invoice
        for invoice in Invoice.objects.filter(pay_url__in=list(groups)).prefetch_related("items")
    }
    sellers = list(Seller.objects.all())
    touched: set[int] = set()
    for pay_url, group in groups.items():
        lines = ", ".join(str(row.row) for row in group)
        where = f"счёт {pay_url} (строки {lines})"
        placements = _placements(group, pools, report)
        if placements is None:
            continue
        total = _total(group)
        if total is None:
            report.issue(Section.INVOICES, f"{where}: нет ни цены, ни цены за пачку — не записан")
            continue
        invoice = existing.get(pay_url)
        if invoice is not None:
            same = (invoice.amount_cents, invoice.currency) == (total, CURRENCY) and {
                item.placement_id for item in invoice.items.all()
            } == {placement.pk for placement in placements}
            if same:
                report.count("invoices", Outcome.UNCHANGED)
            else:
                report.issue(
                    Section.INVOICES,
                    f"{where}: в базе счёт по этой ссылке другой"
                    " (сумма или размещения) — не тронут",
                )
            continue
        seller = _seller(group, placements, sellers, report, where)
        if seller is None:
            continue
        given = [row.price_cents for row in group]
        if any(price is None for price in given) or sum(p or 0 for p in given) != total:
            if any(price is not None for price in given) and len(group) > 1:
                report.issue(
                    Section.INVOICES,
                    f"{where}: цены строк не дают цену за пачку — поделено поровну",
                )
            given = [None] * len(group)
        shares = invoices.shares(total, given, CURRENCY)
        invoice = Invoice.objects.create(
            seller=seller,
            amount_cents=total,
            currency=CURRENCY,
            pay_url=pay_url,
            issued_on=_issued_on(placements, as_of),
            status=InvoiceStatus.PAID,
            comment=COMMENT,
        )
        InvoiceItem.objects.bulk_create(
            InvoiceItem(invoice=invoice, placement=placement, amount_cents=share)
            for placement, share in zip(placements, shares, strict=True)
        )
        touched.update(placement.pk for placement in placements)
        report.count("invoices", Outcome.CREATED)
        report.count("invoice_items", Outcome.CREATED, len(placements))
    invoices.sync_paid(touched)


def _placements(
    group: list[InvoiceRow], pools: dict[str, list[Placement]], report: Report
) -> list[Placement] | None:
    """Размещения строк счёта; хоть одно не нашлось — счёт не записывается."""
    found: list[Placement] = []
    for row in group:
        match = match_placement(pools.get(row.domain, []), row.article_url)
        if match.placement is None or match.placement.pk is None:
            why = "их несколько" if match.ambiguous else "нет"
            report.issue(
                Section.INVOICES,
                f"строка {row.row}: размещения Convertio на {row.domain} по ссылке на статью"
                f" {why} — счёт не записан",
            )
            return None
        if match.placement in found:
            report.issue(
                Section.INVOICES,
                f"строка {row.row}: размещение {row.domain} в счёте дважды — счёт не записан",
            )
            return None
        found.append(match.placement)
    return found


def _total(group: list[InvoiceRow]) -> int | None:
    batch = next((row.batch_cents for row in group if row.batch_cents), None)
    if batch:
        return batch
    prices = [row.price_cents for row in group]
    if any(price is None for price in prices):
        return None
    return sum(price or 0 for price in prices) or None


def _seller(
    group: list[InvoiceRow],
    placements: list[Placement],
    sellers: list[Seller],
    report: Report,
    where: str,
) -> Seller | None:
    """Продавец размещений, иначе — по «Вебмастеру\\агентству»; почта — в контакты."""
    contact = next((row.contact for row in group if row.contact), None)
    own = {placement.seller_id for placement in placements} - {None}
    seller: Seller | None = None
    if len(own) > 1:
        report.issue(Section.INVOICES, f"{where}: размещения у разных продавцов — не записан")
        return None
    if own:
        seller = next(s for s in sellers if s.pk in own)
    elif contact is not None:
        seller = _by_contact(sellers, contact)
        if seller is None:
            seller = Seller.objects.create(name=contact, currency=CURRENCY)
            sellers.append(seller)
            report.count("sellers", Outcome.CREATED)
    if seller is None:
        report.issue(Section.INVOICES, f"{where}: не понять продавца — не записан")
        return None
    if (
        contact is not None
        and "@" in contact
        and contact.lower() not in (seller.contacts or "").lower()
        and contact.lower() != seller.name.lower()
    ):
        seller.contacts = "\n".join(part for part in (seller.contacts, contact) if part)
        seller.save(update_fields=["contacts"])
        report.count("sellers", Outcome.UPDATED)
    return seller


def _by_contact(sellers: list[Seller], contact: str) -> Seller | None:
    needle = contact.lower()
    for seller in sellers:
        if seller.name.lower() == needle or needle in (seller.contacts or "").lower():
            return seller
    return None


def _issued_on(placements: list[Placement], as_of: dt.date) -> dt.date:
    days = [timezone.localdate(p.published_at) for p in placements if p.published_at is not None]
    return min(days) if days else as_of
