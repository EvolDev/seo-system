"""Импорт вкладки «Счета» таблицы (E1-14, ADR-055, маппинг §1.9)."""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement
from apps.sites.importing.report import Outcome, Report, Section
from apps.sites.importing.run import ImportOptions, run_import
from apps.sites.models import Product, Seller

pytestmark = pytest.mark.django_db

MakeWorkbook = Callable[..., Path]
AS_OF = dt.date(2026, 10, 4)
PAY = "https://www.paypal.com/invoice/p/#CL9SJFQ7NL4VUWU7"
DEV = {
    "Статус": "Размещено",
    "URL статьи": "https://dev.to/ben_blog/licence",
    "Дата размещения": "30.09.2026",
    "Источник": "StarMedia",
    "Итог цена": 181,
    "Анкор1": "heic to jpg",
    "Ссылка1": "https://convertio.co/heic-jpg/",
}
# Строка листа от 04.10.2026: цена с запятой, «---» в средней цене.
DEV_INVOICE = {
    "Площадка": "dev.to",
    "Ссылка на статью": "https://dev.to/ben_blog/licence",
    "Цена EUR": "181,00",
    "Ссылка на оплату": PAY,
    "Вебмастер\\агентство": "Alexander@star5media.com",
    "Средняя цена за сайт": "---",
}


@pytest.fixture(autouse=True)
def products() -> tuple[Product, Product]:
    return (
        Product.objects.create(name="Convertio", domain="convertio.co"),
        Product.objects.create(name="Clideo", domain="clideo.com"),
    )


@pytest.fixture
def run(tmp_path: Path) -> Callable[[Path], Report]:
    def run_(path: Path) -> Report:
        options = ImportOptions(path=path, as_of=AS_OF, list_name="Октябрь", dry_run=False)
        return run_import(options, tmp_path / "reports").report

    return run_


def _site(domain: str, **fields: object) -> tuple[str, dict[str, object]]:
    return domain, {**DEV, "URL статьи": f"https://{domain}/post", **fields}


def test_single_invoice(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    report = run(make_workbook(base=[("dev.to", DEV)], invoices=[DEV_INVOICE]))
    invoice = Invoice.objects.get()
    assert (invoice.amount_cents, invoice.currency, invoice.pay_url) == (18100, "EUR", PAY)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_on is None
    assert invoice.issued_on == dt.date(2026, 9, 30)
    assert invoice.seller.name == "StarMedia"
    # Почта вебмастера — в контакты продавца размещения.
    assert invoice.seller.contacts == "Alexander@star5media.com"
    placement = Placement.objects.get()
    assert list(invoice.items.values_list("placement_id", "amount_cents")) == [
        (placement.pk, 18100)
    ]
    assert (placement.price_paid_cents, placement.currency) == (18100, "EUR")
    assert report.counts["invoices"][Outcome.CREATED] == 1


def test_repeat_without_duplicates(
    make_workbook: MakeWorkbook, run: Callable[[Path], Report]
) -> None:
    book = make_workbook(base=[("dev.to", DEV)], invoices=[DEV_INVOICE])
    run(book)
    report = run(book)
    assert Invoice.objects.count() == 1
    assert InvoiceItem.objects.count() == 1
    assert report.counts["invoices"][Outcome.UNCHANGED] == 1
    assert Seller.objects.get(name="StarMedia").contacts == "Alexander@star5media.com"


def test_changed_invoice_reported_not_touched(
    make_workbook: MakeWorkbook, run: Callable[[Path], Report]
) -> None:
    run(make_workbook(base=[("dev.to", DEV)], invoices=[DEV_INVOICE], name="1.xlsx"))
    changed = {**DEV_INVOICE, "Цена EUR": 190}
    report = run(make_workbook(base=[("dev.to", DEV)], invoices=[changed], name="2.xlsx"))
    assert Invoice.objects.get().amount_cents == 18100
    assert report.issues[Section.INVOICES] == [
        f"счёт {PAY} (строки 2): в базе счёт по этой ссылке другой"
        " (сумма или размещения) — не тронут"
    ]


def test_batch_split_evenly(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    base = [_site("a.com"), _site("b.com"), _site("c.com")]
    rows: list[dict[str, object]] = [
        {"Площадка": domain, "Ссылка на статью": f"https://{domain}/post", "Ссылка на оплату": "x"}
        for domain in ("a.com", "b.com", "c.com")
    ]
    rows[0]["Цена за пачку EUR"] = 100
    report = run(make_workbook(base=base, invoices=rows))
    invoice = Invoice.objects.get()
    assert invoice.amount_cents == 10000
    shares = dict(invoice.items.values_list("placement__site__domain", "amount_cents"))
    assert shares == {"a.com": 3334, "b.com": 3333, "c.com": 3333}
    assert Section.INVOICES not in report.issues


def test_batch_with_prices(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    base = [_site("a.com"), _site("b.com")]
    rows = [
        {"Площадка": "a.com", "Ссылка на статью": "https://a.com/post", "Цена EUR": 60},
        {"Площадка": "b.com", "Ссылка на статью": "https://b.com/post", "Цена EUR": 40},
    ]
    for row in rows:
        row.update({"Ссылка на оплату": "x", "Цена за пачку EUR": 100})
    run(make_workbook(base=base, invoices=rows))
    items = Invoice.objects.get().items
    shares = dict(items.values_list("placement__site__domain", "amount_cents"))
    assert shares == {"a.com": 6000, "b.com": 4000}


def test_unknown_placement_skips_invoice(
    make_workbook: MakeWorkbook, run: Callable[[Path], Report]
) -> None:
    row = {**DEV_INVOICE, "Ссылка на статью": "https://dev.to/other"}
    report = run(make_workbook(base=[_site("a.com")], invoices=[row]))
    assert not Invoice.objects.exists()
    assert report.issues[Section.INVOICES] == [
        "строка 2: размещения Convertio на dev.to по ссылке на статью нет — счёт не записан"
    ]


def test_no_pay_url(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    row = {**DEV_INVOICE, "Ссылка на оплату": None}
    report = run(make_workbook(base=[("dev.to", DEV)], invoices=[row]))
    assert not Invoice.objects.exists()
    assert report.issues[Section.INVOICES] == ["строка 2: нет ссылки на оплату — не записана"]


def test_seller_from_contact(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    # У размещения нет «Источника» — продавец по «Вебмастеру\\агентству».
    base = [("dev.to", {**DEV, "Источник": None})]
    known = Seller.objects.create(name="star5media", contacts="Alexander@star5media.com")
    run(make_workbook(base=base, invoices=[DEV_INVOICE]))
    invoice = Invoice.objects.get()
    assert invoice.seller == known
    assert Placement.objects.get().seller == known


def test_no_sheet_note(make_workbook: MakeWorkbook, run: Callable[[Path], Report]) -> None:
    report = run(make_workbook(base=[("dev.to", DEV)]))
    assert "Вкладки «Счета» нет — счета не загружались" in report.notes


def test_table_paid_does_not_override_invoice(
    make_workbook: MakeWorkbook, run: Callable[[Path], Report]
) -> None:
    run(make_workbook(base=[("dev.to", DEV)], invoices=[DEV_INVOICE], name="1.xlsx"))
    changed = {**DEV, "Итог цена": 190}
    report = run(make_workbook(base=[("dev.to", changed)], name="2.xlsx"))
    assert Placement.objects.get().price_paid_cents == 18100
    assert (
        "dev.to (строка 2): «заплачено» в базе €181 (из счёта), в таблице «Итог цена» €190"
        in report.issues[Section.PLACEMENT_CONFLICTS]
    )
