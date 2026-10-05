"""Статистика на главной: размещено и потрачено по месяцам (E1-14)."""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.placements import home, stats
from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement, PlacementStatus
from apps.sites.models import ExchangeRate, Product, Seller, Site

pytestmark = pytest.mark.django_db

MSK = ZoneInfo("Europe/Moscow")
INDEX = reverse("admin:index")


@pytest.fixture(autouse=True)
def moscow() -> Iterator[None]:
    with override_settings(TIME_ZONE="Europe/Moscow"):
        yield


@pytest.fixture
def products() -> tuple[Product, Product]:
    return (
        Product.objects.create(name="Convertio", domain="convertio.co"),
        Product.objects.create(name="Clideo", domain="clideo.com"),
    )


def _published(
    product: Product,
    domain: str,
    when: dt.datetime | None,
    cents: int | None,
    currency: str = "EUR",
) -> Placement:
    return Placement.objects.create(
        site=Site.objects.create(domain=domain),
        product=product,
        status=PlacementStatus.PUBLISHED,
        published_at=when,
        price_paid_cents=cents,
        currency=currency,
    )


@pytest.fixture
def data(products: tuple[Product, Product]) -> tuple[Product, Product]:
    convertio, clideo = products
    ExchangeRate.objects.create(
        currency="USD", rate_date=dt.date(2026, 10, 1), rate=Decimal("1.25")
    )
    # Сентябрь: два Convertio без счёта, один по счёту; 30.09 23:30 по Москве — сентябрь.
    _published(convertio, "a.com", dt.datetime(2026, 9, 11, 12, tzinfo=MSK), 58182)
    _published(convertio, "b.com", dt.datetime(2026, 9, 30, 23, 30, tzinfo=MSK), 10000)
    dev = _published(convertio, "dev.to", dt.datetime(2026, 9, 30, 12, tzinfo=MSK), 18100)
    seller = Seller.objects.create(name="StarMedia")
    invoice = Invoice.objects.create(seller=seller, amount_cents=18100)
    InvoiceItem.objects.create(invoice=invoice, placement=dev, amount_cents=18100)
    # Октябрь: Clideo в долларах — пересчёт по курсу, «≈».
    _published(clideo, "c.com", dt.datetime(2026, 10, 1, 0, 30, tzinfo=MSK), 12500, "USD")
    # Без даты и заявка — в месяцы не попадают.
    _published(clideo, "d.com", None, 999)
    Placement.objects.create(
        site=Site.objects.create(domain="e.com"),
        product=convertio,
        status=PlacementStatus.ORDERED,
        published_at=dt.datetime(2026, 9, 5, tzinfo=MSK),
        price_paid_cents=5000,
    )
    return convertio, clideo


def test_months(data: tuple[Product, Product]) -> None:
    months = stats.months(2026, None)
    assert len(months) == 12
    september, october = months[8], months[9]
    assert (september.placed, september.invoiced_cents, september.other_cents) == (3, 18100, 68182)
    assert not september.converted
    assert (october.placed, october.invoiced_cents, october.other_cents) == (1, 0, 10000)
    assert october.converted
    assert sum(m.placed for m in months) == 4
    assert stats.without_date(None) == 1


def test_product_filter(data: tuple[Product, Product]) -> None:
    convertio, clideo = data
    assert [m.placed for m in stats.months(2026, convertio.pk)][8:10] == [3, 0]
    assert [m.placed for m in stats.months(2026, clideo.pk)][8:10] == [0, 1]


def test_this_and_previous_across_year(data: tuple[Product, Product]) -> None:
    current, previous = stats.this_and_previous(dt.date(2026, 10, 4), None)
    assert (current.month, current.placed) == (dt.date(2026, 10, 1), 1)
    assert (previous.month, previous.placed) == (dt.date(2026, 9, 1), 3)
    january, december = stats.this_and_previous(dt.date(2027, 1, 15), None)
    assert (january.month, december.month) == (dt.date(2027, 1, 1), dt.date(2026, 12, 1))


def test_nice_ticks() -> None:
    assert stats.nice_ticks(52) == [0, 20, 40, 60]
    assert stats.nice_ticks(3) == [0, 1, 2, 3]
    assert stats.nice_ticks(0) == [0, 1]
    assert stats.nice_ticks(1623591)[-1] == 2000000


def test_euros() -> None:
    assert home.euros(1623591) == "€16 236"
    assert home.euros(10000, approx=True) == "≈€100"


@pytest.fixture
def october(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "localdate", lambda *args: dt.date(2026, 10, 4))


def test_home_page(admin_client: Client, data: tuple[Product, Product], october: None) -> None:
    convertio, _ = data
    # «Все продукты» — явным выбором: без него — рабочий продукт (ADR-057).
    response = admin_client.get(INDEX, {"product": "all"})
    s = response.context["home_stats"]
    assert s["year"] == 2026
    labels = [(t["label"], t["value"], t["note"]) for t in s["tiles"]]
    assert labels[0] == ("Размещено в октябре", "1", "в сентябре: 3")
    assert labels[1] == ("Потрачено в октябре", "≈€100", "в сентябре: €863")
    assert labels[2] == ("К оплате по счетам", "€181", "счетов: 1")
    september = s["placed"].bars[8]
    assert (
        september.url
        == reverse("admin:placements_placement_changelist")
        + "?month=2026-09&product__id__exact=all"
    )
    assert september.labeled and september.text == "3"
    assert s["placed"].bars[9].current
    spent = s["spent"].bars[8]
    assert [key for key, _share, _text in spent.parts] == ["invoiced", "other"]
    assert s["no_date"] == 1
    page = response.content.decode()
    assert "Размещено по месяцам" in page and "Потрачено по месяцам" in page
    # Высоты — с точкой, а не с запятой русской локали: иначе CSS их не поймёт.
    assert 'style="height: 100.0%"' in page or 'style="height: 100%"' in page
    assert "," not in page.split('class="seo-col-stack" style="height: ')[1].split("%")[0]
    # Выбор продукта — ссылками с продуктом и годом; выбранный — залит.
    assert f"?product={convertio.pk}&amp;year=2026" in page
    chips = page.split('class="seo-stats-products"')[1].split("</div>")[0]
    chosen = 'class="seo-btn seo-btn-primary" href="/admin/?product=all&amp;year=2026"'
    assert f'{chosen} aria-current="true"' in chips


def test_home_product_and_year(
    admin_client: Client, data: tuple[Product, Product], october: None
) -> None:
    _, clideo = data
    s = admin_client.get(INDEX, {"product": str(clideo.pk), "year": "2026"}).context["home_stats"]
    assert s["tiles"][0]["value"] == "1"
    assert s["placed"].bars[8].value == 0
    assert s["placed"].bars[9].url.endswith(f"month=2026-10&product__id__exact={clideo.pk}")
    # Будущий год и мусор — текущий год; раньше первой публикации не листается.
    assert admin_client.get(INDEX, {"year": "2030"}).context["home_stats"]["year"] == 2026
    assert admin_client.get(INDEX, {"year": "x"}).context["home_stats"]["year"] == 2026
    assert s["prev_year_url"] == ""
    assert s["next_year_url"] == ""


def test_cancelled_invoice_not_counted(data: tuple[Product, Product]) -> None:
    Invoice.objects.update(status=InvoiceStatus.CANCELLED)
    september = stats.months(2026, None)[8]
    assert september.invoiced_cents == 0
