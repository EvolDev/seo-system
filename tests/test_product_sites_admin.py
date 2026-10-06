"""Экран «Площадки продукта» поверх `v_product_site_latest` (E9-01).

Фильтры, поиск по вставленному адресу, показ цен и число запросов на
страницу. Что считает само представление — `test_report_views`.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.admin.views.main import ChangeList
from django.test import Client
from django.urls import reverse
from pytest_django import DjangoAssertNumQueries

from apps.content.models import DomainSetting
from apps.placements.models import Placement, PlacementStatus
from apps.sites.admin import SellerFilter, _euros
from apps.sites.models import (
    AuditAuthor,
    AuditVerdict,
    ExchangeRate,
    Product,
    ProductSite,
    Seller,
    Site,
    SiteAudit,
    SiteCountryMetric,
    SiteList,
    SiteListItem,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
)

pytestmark = pytest.mark.django_db

OfferFactory = Callable[..., SitePrice]

URL = reverse("admin:sites_productsitelatest_changelist")
EARLY = datetime(2026, 8, 1, tzinfo=UTC)
LATE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


def _site(domain: str, **metrics: Any) -> Site:
    site = Site.objects.create(domain=domain, language=metrics.pop("language", "en"))
    if metrics:
        SiteMetric.objects.create(site=site, **metrics)
    return site


def _domains(client: Client, **params: str) -> set[str]:
    response = client.get(URL, params)
    assert response.status_code == 200
    changelist: ChangeList = response.context["cl"]
    return {row.domain for row in changelist.result_list}


def _sellers(client: Client, **params: str) -> list[str]:
    """Пункты фильтра «продавец» — с числом площадок у имени."""
    response = client.get(URL, params)
    assert response.status_code == 200
    changelist: ChangeList = response.context["cl"]
    spec = next(s for s in changelist.filter_specs if isinstance(s, SellerFilter))
    return [str(choice["display"]) for choice in spec.choices(changelist)]


def _status(site: Site, product: Product, status: SiteStatus) -> None:
    ProductSite.objects.filter(site=site, product=product).update(status=status)


class TestProductAndList:
    def test_default_is_first_active_product(
        self, admin_client: Client, convertio: Product, clideo: Product
    ) -> None:
        _site("a.com")
        response = admin_client.get(URL, {"list": "all"})
        rows = list(response.context["cl"].result_list)
        assert [row.product_id for row in rows] == [convertio.pk]

    def test_inactive_product_is_not_default(
        self, admin_client: Client, convertio: Product, clideo: Product
    ) -> None:
        convertio.is_active = False
        convertio.save()
        _site("a.com")
        response = admin_client.get(URL, {"list": "all"})
        assert [row.product_id for row in response.context["cl"].result_list] == [clideo.pk]

    def test_other_product_by_filter(
        self, admin_client: Client, convertio: Product, clideo: Product
    ) -> None:
        site = _site("a.com")
        _status(site, clideo, SiteStatus.DISCARDED)
        response = admin_client.get(URL, {"list": "all", "product": str(clideo.pk)})
        [row] = response.context["cl"].result_list
        assert row.status == SiteStatus.DISCARDED

    def test_default_list_is_newest(self, admin_client: Client, convertio: Product) -> None:
        old, new = SiteList.objects.create(name="Август"), SiteList.objects.create(name="Сентябрь")
        SiteListItem.objects.create(site_list=old, site=_site("old.com"))
        SiteListItem.objects.create(site_list=new, site=_site("new.com"))
        _site("nowhere.com")
        assert _domains(admin_client) == {"new.com"}
        assert _domains(admin_client, list=str(old.pk)) == {"old.com"}
        assert _domains(admin_client, list="all") == {"old.com", "new.com", "nowhere.com"}

    def test_without_lists_shows_all(self, admin_client: Client, convertio: Product) -> None:
        _site("a.com")
        assert _domains(admin_client) == {"a.com"}

    def test_garbage_parameters_show_nothing(
        self, admin_client: Client, convertio: Product
    ) -> None:
        _site("a.com")
        assert _domains(admin_client, product="abc", list="all") == set()
        assert _domains(admin_client, list="zzz") == set()


class TestWorked:
    """«Уже работали» — по выбранному продукту (ADR-033)."""

    @pytest.fixture
    def sites(self, convertio: Product, clideo: Product) -> None:
        _site("fresh.com")
        _status(_site("decided.com"), convertio, SiteStatus.DISCARDED)
        SiteAudit.objects.create(
            site=_site("audited.com"),
            product=convertio,
            verdict=AuditVerdict.YES,
            author=AuditAuthor.HUMAN,
        )
        Placement.objects.create(site=_site("planned.com"), product=convertio)
        # Статья Clideo для Convertio «уже работали» не делает.
        Placement.objects.create(
            site=_site("clideo.com"), product=clideo, status=PlacementStatus.PLACED
        )

    @pytest.mark.usefixtures("sites")
    def test_worked(self, admin_client: Client) -> None:
        assert _domains(admin_client, list="all", worked="yes") == {
            "decided.com",
            "audited.com",
            "planned.com",
        }

    @pytest.mark.usefixtures("sites")
    def test_new_for_us(self, admin_client: Client) -> None:
        assert _domains(admin_client, list="all", worked="no") == {"fresh.com", "clideo.com"}

    @pytest.mark.usefixtures("sites")
    def test_other_product_shown_as_link(self, admin_client: Client) -> None:
        content = admin_client.get(URL, {"list": "all", "q": "clideo.com"}).content.decode()
        site = Site.objects.get(domain="clideo.com")
        link = reverse("admin:placements_placement_changelist") + f"?site__id__exact={site.pk}"
        assert f'href="{link}">Clideo</a>' in content


class TestFilters:
    @pytest.fixture
    def sites(self, convertio: Product) -> None:
        low = _site("low.com", dr=10, organic_traffic=100)
        mid = _site("mid.com", dr=50, organic_traffic=5000, language="de")
        _site("high.com", dr=90, organic_traffic=90000)
        _status(low, convertio, SiteStatus.IN_WORK)
        Placement.objects.create(site=mid, product=convertio, status=PlacementStatus.PLACED)

    @pytest.mark.usefixtures("sites")
    def test_dr_range(self, admin_client: Client) -> None:
        found = _domains(admin_client, list="all", dr__range__gte="20", dr__range__lte="60")
        assert found == {"mid.com"}

    @pytest.mark.usefixtures("sites")
    def test_traffic_range(self, admin_client: Client) -> None:
        found = _domains(admin_client, list="all", organic_traffic__range__gte="1000")
        assert found == {"mid.com", "high.com"}

    @pytest.mark.usefixtures("sites")
    def test_status(self, admin_client: Client) -> None:
        assert _domains(admin_client, list="all", status="in_work") == {"low.com"}

    @pytest.mark.usefixtures("sites")
    def test_language(self, admin_client: Client) -> None:
        assert _domains(admin_client, list="all", language="de") == {"mid.com"}

    @pytest.mark.usefixtures("sites")
    def test_published(self, admin_client: Client) -> None:
        assert _domains(admin_client, list="all", published="yes") == {"mid.com"}
        assert _domains(admin_client, list="all", published="no") == {"low.com", "high.com"}

    def test_seller_counts_follow_other_filters(
        self, admin_client: Client, convertio: Product, offer: OfferFactory
    ) -> None:
        """У имени продавца — сколько его площадок в списке при остальных фильтрах."""
        hub = Seller.objects.create(name="LinkHub Media", currency="USD")
        Seller.objects.create(name="Quiet Links", currency="USD")
        offer(_site("low.com", dr=10), 10000, seller=hub, working=False)
        offer(_site("high.com", dr=90), 10000, seller=hub, working=False)
        found = _sellers(admin_client, list="all")
        assert "LinkHub Media (2)" in found
        # Продавца, у которого в этом списке площадок нет, в фильтре не показываем.
        assert not [name for name in found if "Quiet Links" in name]
        narrowed = _sellers(admin_client, list="all", dr__range__gte="50")
        assert "LinkHub Media (1)" in narrowed

    def test_chosen_seller_stays_when_nothing_left(
        self, admin_client: Client, convertio: Product, offer: OfferFactory
    ) -> None:
        """Выбранный продавец виден с нулём — иначе фильтр нечем снять."""
        hub = Seller.objects.create(name="LinkHub Media", currency="USD")
        offer(_site("low.com", dr=10), 10000, seller=hub, working=False)
        found = _sellers(admin_client, list="all", seller=str(hub.pk), dr__range__gte="50")
        assert "LinkHub Media (0)" in found

    def test_writing(self, admin_client: Client, convertio: Product, offer: OfferFactory) -> None:
        # «Написание» вместо «пишем мы» (ADR-043): предлагает ли площадка статью.
        offer(_site("writes.com"), 10000, writing_cents=9000)
        offer(_site("no-writing.com"), 10000)
        _site("no-price.com")
        assert _domains(admin_client, list="all", writing="yes") == {"writes.com"}
        assert _domains(admin_client, list="all", writing="no") == {"no-writing.com"}


class TestSellerMultiFilter:
    """Фильтр продавцов — галочками, несколько сразу (просьба 05.10.2026)."""

    @pytest.fixture
    def two_sellers(self, convertio: Product, offer: OfferFactory) -> tuple[Seller, Seller]:
        hub = Seller.objects.create(name="LinkHub Media", currency="USD")
        quiet = Seller.objects.create(name="Quiet Links", currency="USD")
        offer(_site("hub.com"), 10000, seller=hub, working=False)
        offer(_site("quiet.com"), 10000, seller=quiet, working=False)
        _site("nobody.com")
        return hub, quiet

    def test_two_sellers_at_once(
        self, admin_client: Client, two_sellers: tuple[Seller, Seller]
    ) -> None:
        hub, quiet = two_sellers
        assert _domains(admin_client, list="all", seller=str(hub.pk)) == {"hub.com"}
        response = admin_client.get(URL, {"list": "all", "seller": [str(hub.pk), str(quiet.pk)]})
        both = {row.domain for row in response.context["cl"].result_list}
        assert both == {"hub.com", "quiet.com"}

    def test_without_choice_everything_is_checked(
        self, admin_client: Client, two_sellers: tuple[Seller, Seller]
    ) -> None:
        # Ничего не выбрано — показаны все, и галочки стоят у всех: так из полного
        # списка убирают лишнего одним щелчком.
        response = admin_client.get(URL, {"list": "all"})
        spec = next(s for s in response.context["cl"].filter_specs if isinstance(s, SellerFilter))
        items = list(spec.choices(response.context["cl"]))
        assert all(item["selected"] for item in items)
        assert len(_domains(admin_client, list="all")) == 3

    def test_all_again_unchecks_everyone(
        self, admin_client: Client, two_sellers: tuple[Seller, Seller]
    ) -> None:
        response = admin_client.get(URL, {"list": "all", "seller": "-"})
        spec = next(s for s in response.context["cl"].filter_specs if isinstance(s, SellerFilter))
        items = list(spec.choices(response.context["cl"]))
        assert not any(item["selected"] for item in items)
        assert len(response.context["cl"].result_list) == 0


class TestPerPage:
    """Сколько строк на странице — выбором человека (просьба 05.10.2026)."""

    @pytest.fixture
    def many(self, convertio: Product) -> None:
        for number in range(120):
            _site(f"site{number}.com", dr=number % 100)

    @pytest.mark.usefixtures("many")
    def test_default_is_hundred(self, admin_client: Client) -> None:
        response = admin_client.get(URL, {"list": "all"})
        assert len(response.context["cl"].result_list) == 100
        assert "На странице:" in response.content.decode()

    @pytest.mark.usefixtures("many")
    def test_chosen_size_applies(self, admin_client: Client) -> None:
        response = admin_client.get(URL, {"list": "all", "per_page": "50"})
        assert len(response.context["cl"].result_list) == 50

    @pytest.mark.usefixtures("many")
    def test_unknown_size_is_ignored_and_filters_still_work(self, admin_client: Client) -> None:
        # Чужой параметр админка приняла бы за отбор по полю — список бы упал.
        response = admin_client.get(URL, {"list": "all", "per_page": "7", "language": "en"})
        assert response.status_code == 200
        assert len(response.context["cl"].result_list) == 100


class TestSearch:
    def test_full_url_finds_site(self, admin_client: Client, convertio: Product) -> None:
        _site("example.com")
        _site("other.com")
        found = _domains(admin_client, list="all", q="https://www.Example.com/blog/post?utm=1")
        assert found == {"example.com"}


class TestPrices:
    def test_euros(self) -> None:
        assert _euros(12000) == "€120"
        assert _euros(12050) == "€120.50"
        assert _euros(5) == "€0.05"
        assert _euros(None) == ""

    def test_prices_and_spend(
        self, admin_client: Client, convertio: Product, offer: OfferFactory
    ) -> None:
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        offer(_site("a.com"), 10000, announce_cents=2000, writing_cents=3000)
        content = admin_client.get(URL, {"list": "all"}).content.decode()
        assert "<b>€100</b>" in content  # цена: размещение, без анонса
        assert "публикация · Collaborator" in content
        assert "📝 €30" in content  # написание
        assert "€150" in content  # плановые расходы: пишет площадка

    def test_no_price_shows_no_price(self, admin_client: Client, convertio: Product) -> None:
        # 0 выглядел бы как «бесплатно».
        DomainSetting.objects.create(key="PRICE_REFERENCE", value={"writing_eur": 50})
        _site("a.com")
        content = admin_client.get(URL, {"list": "all"}).content.decode()
        assert "€0" not in content
        assert "нет цены" in content


class TestReadOnly:
    def test_no_add_change_delete_removes_site(
        self, admin_client: Client, convertio: Product
    ) -> None:
        site = _site("a.com")
        response = admin_client.get(URL, {"list": "all"})
        model_admin = response.context["cl"].model_admin
        request = response.wsgi_request
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        # «Удалить отмеченные» удаляет саму площадку (ADR-060).
        assert model_admin.has_delete_permission(request)
        assert model_admin.delete_roots(response.context["cl"].result_list) == {Site: [site.pk]}


def test_query_count(
    admin_client: Client,
    convertio: Product,
    clideo: Product,
    offer: OfferFactory,
    django_assert_max_num_queries: DjangoAssertNumQueries,
) -> None:
    """Критерий приёмки: страница — не больше 14 запросов при любом числе строк.

    С E1-07 — ещё и с продавцами, предложениями, заметками и их фильтрами.
    С E1-10 — с выбранным регионом и его «от/до»: список регионов — 11-й
    запрос, колонки региона — подзапросы в основном. С E9-10 — «Мои фильтры»:
    наборы пользователя на этом списке — 12-й. С E9-12 — рабочий продукт
    пользователя — 13-й (список продуктов — общий у шапки и фильтра).
    С ADR-060 — счётчики площадок у продавцов в фильтре: 14-й, один на всех
    продавцов.
    """
    site_list = SiteList.objects.create(name="Сентябрь")
    seller = Seller.objects.create(name="LinkHub Media", currency="USD")
    for number in range(150):
        site = _site(f"site{number}.com", dr=number % 100, organic_traffic=number * 10)
        offer(site, 1000 + number, writing_cents=500)
        offer(site, 900 + number, seller=seller, currency="USD", working=False)
        SiteNote.objects.create(site=site, body=f"заметка {number}")
        SiteListItem.objects.create(site_list=site_list, site=site)
        SiteCountryMetric.objects.create(site=site, country="us", organic_traffic=number)
        if number % 3 == 0:
            Placement.objects.create(site=site, product=clideo, status=PlacementStatus.PLACED)
    ExchangeRate.objects.create(currency="USD", rate_date=datetime(2026, 9, 30).date(), rate=1.1355)
    params = {
        "worked": "no",
        "dr__range__gte": "0",
        "organic_traffic__range__gte": "0",
        "language": "en",
        "published": "no",
        "offers": "cheaper",
        "seller": str(seller.pk),
        "notes": "yes",
        "writing": "yes",
        "region": "us",
        "region_traffic__range__gte": "0",
    }
    # Тема Admin Interface при первом открытии заводит свою строку и кладёт её
    # в кеш; в работающем приложении она там уже есть, считаем без неё.
    admin_client.get(URL)
    with django_assert_max_num_queries(14):
        response = admin_client.get(URL, params)
    assert response.status_code == 200
    assert len(response.context["cl"].result_list) == 100
    assert "трафик US" in response.content.decode()
