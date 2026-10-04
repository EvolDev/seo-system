"""Счета продавцов: доли и «Заплачено» из счёта (E1-14, ADR-055)."""

import pytest
from django.db import IntegrityError, transaction

from apps.placements.invoices import ShareError, invoiced, item_errors, shares, sync_paid
from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement
from apps.sites.models import Product, Seller, Site


class TestShares:
    def test_batch_split_evenly_to_the_cent(self) -> None:
        # Лишний цент — первым строкам: сумма долей ровно сумма счёта.
        assert shares(10000, [None, None, None], "EUR") == [3334, 3333, 3333]

    def test_single_placement_gets_whole_sum(self) -> None:
        assert shares(18100, [None], "EUR") == [18100]

    def test_given_share_kept_rest_split(self) -> None:
        assert shares(10000, [5000, None, None], "EUR") == [5000, 2500, 2500]

    def test_all_given_and_match(self) -> None:
        assert shares(10000, [6000, 4000], "EUR") == [6000, 4000]

    def test_all_given_short(self) -> None:
        with pytest.raises(ShareError, match="меньше суммы счёта на €2"):
            shares(10000, [6000, 3800], "EUR")

    def test_given_more_than_total(self) -> None:
        with pytest.raises(ShareError, match="больше суммы счёта на \\$5"):
            shares(10000, [10500, None], "USD")

    def test_no_rows(self) -> None:
        # Счёт без размещений: продавец выставил, заявки заведут позже.
        assert shares(10000, [], "EUR") == []

    def test_zero_share_allowed(self) -> None:
        # Площадка в пачке бесплатно — доля 0.
        assert shares(10000, [0, None], "EUR") == [0, 10000]
        assert shares(10000, [10000, 0], "EUR") == [10000, 0]

    def test_new_row_when_shares_already_full(self) -> None:
        # Добавили размещение в счёт, где доли уже дают всю сумму, — не молча 0.
        with pytest.raises(ShareError, match="пустым строкам ничего не остаётся"):
            shares(10000, [5000, 5000, None], "EUR")


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def seller() -> Seller:
    return Seller.objects.create(name="StarMedia")


def _placement(product: Product, domain: str, **fields: object) -> Placement:
    site = Site.objects.create(domain=domain)
    return Placement.objects.create(site=site, product=product, **fields)


def _invoice(seller: Seller, amount: int, **fields: object) -> Invoice:
    return Invoice.objects.create(seller=seller, amount_cents=amount, **fields)


@pytest.mark.django_db
class TestSyncPaid:
    def test_paid_from_shares(self, product: Product, seller: Seller) -> None:
        first = _placement(product, "dev.to")
        second = _placement(product, "hashnode.com", price_paid_cents=999)
        invoice = _invoice(seller, 10001, currency="USD")
        InvoiceItem.objects.create(invoice=invoice, placement=first, amount_cents=5001)
        InvoiceItem.objects.create(invoice=invoice, placement=second, amount_cents=5000)
        sync_paid({first.pk, second.pk})
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.price_paid_cents, first.currency) == (5001, "USD")
        # Сумма, вписанная руками до счёта, уступает счёту.
        assert (second.price_paid_cents, second.currency) == (5000, "USD")

    def test_seller_set_from_invoice(self, product: Product, seller: Seller) -> None:
        placement = _placement(product, "dev.to")
        invoice = _invoice(seller, 18100)
        InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=18100)
        sync_paid({placement.pk})
        placement.refresh_from_db()
        assert placement.seller == seller

    def test_two_invoices_sum(self, product: Product, seller: Seller) -> None:
        # Предоплата и остаток — «Заплачено» сумма двух долей.
        placement = _placement(product, "dev.to")
        for amount in (9000, 9100):
            invoice = _invoice(seller, amount)
            InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=amount)
        sync_paid({placement.pk})
        placement.refresh_from_db()
        assert placement.price_paid_cents == 18100

    def test_cancelled_invoice_clears_paid(self, product: Product, seller: Seller) -> None:
        placement = _placement(product, "dev.to")
        invoice = _invoice(seller, 18100)
        InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=18100)
        sync_paid({placement.pk})
        invoice.status = InvoiceStatus.CANCELLED
        invoice.save()
        sync_paid({placement.pk})
        placement.refresh_from_db()
        assert placement.price_paid_cents is None
        assert invoiced([placement.pk]) == set()

    def test_invoiced(self, product: Product, seller: Seller) -> None:
        inside = _placement(product, "dev.to")
        outside = _placement(product, "hashnode.com")
        invoice = _invoice(seller, 18100)
        InvoiceItem.objects.create(invoice=invoice, placement=inside, amount_cents=18100)
        assert invoiced([inside.pk, outside.pk]) == {inside.pk}


@pytest.mark.django_db
class TestItemErrors:
    def test_other_seller(self, product: Product, seller: Seller) -> None:
        other = Seller.objects.create(name="Authlinker")
        placement = _placement(product, "dev.to", seller=other)
        invoice = Invoice(seller=seller, amount_cents=100)
        errors = item_errors(invoice, [placement], currency="EUR", seller_id=seller.pk)
        assert errors == [
            "dev.to куплено у продавца «Authlinker» — в счёт другого продавца его не записать."
        ]

    def test_no_seller_ok(self, product: Product, seller: Seller) -> None:
        placement = _placement(product, "dev.to")
        invoice = Invoice(seller=seller, amount_cents=100)
        assert item_errors(invoice, [placement], currency="EUR", seller_id=seller.pk) == []

    def test_other_currency_in_other_invoice(self, product: Product, seller: Seller) -> None:
        placement = _placement(product, "dev.to")
        dollars = _invoice(seller, 100, currency="USD", number="7")
        InvoiceItem.objects.create(invoice=dollars, placement=placement, amount_cents=100)
        invoice = Invoice(seller=seller, amount_cents=100)
        errors = item_errors(invoice, [placement], currency="EUR", seller_id=seller.pk)
        assert len(errors) == 1
        assert "уже в другом счёте (счёт № 7 от" in errors[0]
        # Тот же счёт и отменённый — не мешают.
        assert item_errors(dollars, [placement], currency="EUR", seller_id=seller.pk) == []
        dollars.status = InvoiceStatus.CANCELLED
        dollars.save()
        assert item_errors(invoice, [placement], currency="EUR", seller_id=seller.pk) == []


@pytest.mark.django_db
class TestInvoiceConstraints:
    def test_defaults(self, seller: Seller) -> None:
        invoice = _invoice(seller, 100)
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.ISSUED
        assert invoice.currency == "EUR"
        assert invoice.issued_on is not None

    def test_amount_positive(self, seller: Seller) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _invoice(seller, 0)

    def test_pay_url_unique(self, seller: Seller) -> None:
        url = "https://www.paypal.com/invoice/p/#CL9SJFQ7NL4VUWU7"
        _invoice(seller, 100, pay_url=url)
        _invoice(seller, 100)
        _invoice(seller, 100)
        with pytest.raises(IntegrityError), transaction.atomic():
            _invoice(seller, 100, pay_url=url)

    def test_placement_once_per_invoice(self, product: Product, seller: Seller) -> None:
        placement = _placement(product, "dev.to")
        invoice = _invoice(seller, 100)
        InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=100)
        with pytest.raises(IntegrityError), transaction.atomic():
            InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=0)
