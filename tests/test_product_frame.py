"""Продукт как рабочая рамка: строки, карточка, «другие продукты» (E1-19, ADR-063).

Продукт в шапке решает, чьи строки в «Площадках» и «Размещениях» и чью карточку
открывает щелчок. Фильтр «продукт» рамку не меняет: он сужает список до
площадок, где работал и выбранный продукт. Сюда же память выбора фильтров —
без неё «Все» по умолчанию было бы неудобно.
"""

from typing import Any

import pytest
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from pytest_django import DjangoAssertNumQueries

from apps.keywords.models import Keyword
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, ProductSite, Seller, Site, SiteList, SiteListItem, SitePrice
from apps.workspace.models import UserSettings
from apps.workspace.products import WorkingProductFilter, choose_product

pytestmark = pytest.mark.django_db

SITES = reverse("admin:sites_productsitelatest_changelist")
KEYWORDS = reverse("admin:keywords_keyword_changelist")
PLACEMENTS = reverse("admin:placements_placement_changelist")
ADD = reverse("admin:placements_placement_add")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def clideo() -> Product:
    return Product.objects.create(name="Clideo", domain="clideo.com")


@pytest.fixture
def both(convertio: Product, clideo: Product) -> dict[str, Any]:
    """Три площадки: общая, только наша и только чужая — как в жизни с Clideo."""
    names = ("both.com", "our.com", "their.com")
    shared, ours, theirs = (Site.objects.create(domain=name) for name in names)
    return {
        "shared": shared,
        "ours": ours,
        "theirs": theirs,
        "our_shared": Placement.objects.create(site=shared, product=convertio),
        # Чужое размещение — не опубликовано: его всё равно видно (E1-19).
        "their_shared": Placement.objects.create(site=shared, product=clideo),
        "our_only": Placement.objects.create(site=ours, product=convertio),
        "their_only": Placement.objects.create(
            site=theirs, product=clideo, status=PlacementStatus.PLACED
        ),
    }


def _rows(client: Client, url: str, **params: str) -> list[Any]:
    response = client.get(url, params)
    assert response.status_code == 200
    changelist: ChangeList = response.context["cl"]
    return list(changelist.result_list)


def _domains(client: Client, url: str, **params: str) -> set[str]:
    rows = _rows(client, url, **params)
    return {row.domain if hasattr(row, "domain") else row.site.domain for row in rows}


class TestFrame:
    @pytest.mark.usefixtures("both")
    def test_rows_are_of_the_working_product(
        self, admin_client: Client, convertio: Product
    ) -> None:
        assert {row.product_id for row in _rows(admin_client, SITES, list="all")} == {convertio.pk}
        assert {row.product_id for row in _rows(admin_client, PLACEMENTS)} == {convertio.pk}

    def test_filter_does_not_bring_other_rows(
        self, admin_client: Client, both: dict[str, Any], clideo: Product
    ) -> None:
        # Выбор чужого продукта — это «где работал и он», а не «покажи его строки».
        assert _domains(admin_client, PLACEMENTS, **{"product__id__exact": str(clideo.pk)}) == {
            "both.com"
        }
        assert _domains(admin_client, SITES, list="all", product=str(clideo.pk)) == {
            "both.com",
            "their.com",
        }

    @pytest.mark.usefixtures("both")
    def test_all_means_do_not_narrow(self, admin_client: Client) -> None:
        assert _domains(admin_client, PLACEMENTS, **{"product__id__exact": "all"}) == {
            "both.com",
            "our.com",
        }
        assert _domains(admin_client, SITES, list="all", product="all") == {
            "both.com",
            "our.com",
            "their.com",
        }

    def test_header_switches_the_frame(
        self, admin_client: Client, admin_user: User, both: dict[str, Any], clideo: Product
    ) -> None:
        choose_product(admin_user, clideo)
        assert _domains(admin_client, PLACEMENTS) == {"both.com", "their.com"}

    def test_other_product_card_opens_by_its_link(
        self, admin_client: Client, both: dict[str, Any]
    ) -> None:
        # Рамка живёт в списке: прямая ссылка на чужую карточку работает.
        url = reverse("admin:placements_placement_change", args=[both["their_shared"].pk])
        assert admin_client.get(url).status_code == 200


class TestOtherProducts:
    def test_same_column_in_both_lists(self, admin_client: Client, both: dict[str, Any]) -> None:
        link = reverse("admin:placements_placement_change", args=[both["their_shared"].pk])
        cell = f'<a href="{link}" data-panel title="Карточка размещения Clideo">Clideo</a>'
        assert cell in admin_client.get(PLACEMENTS).content.decode()
        assert cell in admin_client.get(SITES, {"list": "all", "q": "both.com"}).content.decode()

    @pytest.mark.usefixtures("both")
    def test_column_is_last_in_placements(self, admin_client: Client) -> None:
        changelist: ChangeList = admin_client.get(PLACEMENTS).context["cl"]
        assert list(changelist.list_display)[-1] == "other_products_cell"
        assert "product" not in changelist.list_display

    def test_queries_do_not_grow_with_rows(
        self,
        admin_client: Client,
        both: dict[str, Any],
        convertio: Product,
        clideo: Product,
        django_assert_max_num_queries: DjangoAssertNumQueries,
    ) -> None:
        """Колонка берёт чужие размещения одним запросом на страницу, а не на строку."""
        admin_client.get(PLACEMENTS)  # тема админки заводит свою строку при первом открытии
        with django_assert_max_num_queries(99) as small:
            admin_client.get(PLACEMENTS)
        for number in range(20):
            site = Site.objects.create(domain=f"site{number}.com")
            Placement.objects.create(site=site, product=convertio)
            Placement.objects.create(site=site, product=clideo)
        with django_assert_max_num_queries(len(small.captured_queries)):
            response = admin_client.get(PLACEMENTS)
        assert len(response.context["cl"].result_list) == 22


class TestWithoutFrame:
    """Экраны без рамки: «Ключи», «Счета». Рамки нет, но «Все» по умолчанию есть."""

    @pytest.fixture
    def keywords(self, convertio: Product, clideo: Product) -> None:
        Keyword.objects.create(product=convertio, keyword="pdf to word", target_url="https://c.co/")
        Keyword.objects.create(product=clideo, keyword="cut video", target_url="https://cl.com/")

    @pytest.mark.usefixtures("keywords")
    def test_all_is_a_value_of_its_own(self, admin_client: Client, clideo: Product) -> None:
        # «Все» должен ставить `all`, а не снимать параметр: пустой параметр
        # теперь значит «как в прошлый раз», и из выбора было бы не выйти.
        response = admin_client.get(KEYWORDS, {"product__id__exact": clideo.pk})
        changelist: ChangeList = response.context["cl"]
        spec = next(s for s in changelist.filter_specs if isinstance(s, WorkingProductFilter))
        first = next(iter(spec.choices(changelist)))
        assert first["display"] == "Все"
        assert "product__id__exact=all" in first["query_string"]

    @pytest.mark.usefixtures("keywords")
    def test_all_comes_back_after_a_choice(self, admin_client: Client, clideo: Product) -> None:
        admin_client.get(KEYWORDS, {"product__id__exact": clideo.pk})
        rows = _rows(admin_client, KEYWORDS, **{"product__id__exact": "all"})
        assert len(rows) == 2


class TestDoor:
    def test_site_without_our_placement_offers_empty_form(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        page = admin_client.get(SITES, {"list": "all", "q": "their.com"}).content.decode()
        url = f"{ADD}?site={both['theirs'].pk}&amp;product={convertio.pk}"
        assert f'href="{url}" data-panel title="Взять в размещение"' in page

    def test_site_with_our_placement_opens_its_card(
        self, admin_client: Client, both: dict[str, Any]
    ) -> None:
        page = admin_client.get(SITES, {"list": "all", "q": "our.com"}).content.decode()
        url = reverse("admin:placements_placement_change", args=[both["our_only"].pk])
        assert f'href="{url}" data-panel title="Карточка размещения"' in page

    def test_placement_row_offers_removal(self, admin_client: Client, both: dict[str, Any]) -> None:
        page = admin_client.get(PLACEMENTS).content.decode()
        url = reverse("admin:placements_placement_delete", args=[both["our_only"].pk])
        assert f'href="{url}" title="Убрать из размещений"' in page

    def test_placement_row_opens_the_record(
        self, admin_client: Client, both: dict[str, Any]
    ) -> None:
        # Домен в «Размещениях» открывает само размещение, как и до E1-20.
        page = admin_client.get(PLACEMENTS).content.decode()
        url = reverse("admin:placements_placement_change", args=[both["our_only"].pk])
        assert f'<a href="{url}" title="Карточка размещения">our.com</a>' in page

    def test_empty_form_knows_the_site_and_the_product(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        response = admin_client.get(ADD, {"site": both["theirs"].pk, "product": convertio.pk})
        initial = response.context["adminform"].form.initial
        assert initial["site"] == str(both["theirs"].pk)
        assert initial["product"] == str(convertio.pk)

    def test_in_work_form_can_be_saved_without_extra_fields(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        before = Placement.objects.count()
        response = admin_client.post(ADD, _untouched(both["theirs"].pk, convertio.pk))
        assert response.status_code == 302
        assert Placement.objects.count() == before + 1
        assert (
            Placement.objects.get(site=both["theirs"], product=convertio).status
            == PlacementStatus.IN_WORK
        )

    def test_one_filled_field_is_enough(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        form = _untouched(both["theirs"].pk, convertio.pk) | {"comment": "начали под Convertio"}
        admin_client.post(ADD, form)
        assert Placement.objects.filter(site=both["theirs"], product=convertio).exists()


class TestTakePlacement:
    """Массовое «Взять в размещение» в «Площадках» (E1-20)."""

    def _rows(self, client: Client) -> list[str]:
        rows = _rows(client, SITES, list="all")
        return [str(row.pk) for row in rows]

    def test_creates_for_sites_without_our_placement(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        seller = Seller.objects.create(name="Working seller")
        price = SitePrice.objects.create(
            site=both["theirs"],
            seller=seller,
            placement_type="link_insertion",
            placement_cents=12300,
        )
        both["theirs"].price = price
        both["theirs"].save(update_fields=["price"])
        response = admin_client.post(
            SITES + "?list=all",
            {"action": "take_placement_action", "_selected_action": self._rows(admin_client)},
            follow=True,
        )
        page = response.content.decode()
        # Своё размещение было у both.com и our.com, новое — только у their.com.
        assert "Взято в размещение: 1" in page
        assert "Пропущено, размещение уже есть: 2" in page
        fresh = Placement.objects.get(site=both["theirs"], product=convertio)
        assert fresh.status == PlacementStatus.IN_WORK
        assert (fresh.seller_id, fresh.placement_type) == (seller.pk, "link_insertion")

    def test_site_status_follows_the_placement(
        self, admin_client: Client, both: dict[str, Any], convertio: Product
    ) -> None:
        admin_client.post(
            SITES + "?list=all",
            {"action": "take_placement_action", "_selected_action": self._rows(admin_client)},
            follow=True,
        )
        # Статус площадки идёт за размещением — общий словарь (ADR-062).
        decision = ProductSite.objects.get(site=both["theirs"], product=convertio)
        assert decision.status == PlacementStatus.IN_WORK

    def test_nothing_new_is_said_plainly(self, admin_client: Client, both: dict[str, Any]) -> None:
        rows = [str(row.pk) for row in _rows(admin_client, SITES, list="all", q="our.com")]
        response = admin_client.post(
            SITES + "?list=all",
            {"action": "take_placement_action", "_selected_action": rows},
            follow=True,
        )
        assert "Взято в размещение: 0" in response.content.decode()


class TestCounter:
    """«N всего» в «Размещениях» — под рабочий продукт, а не по обоим (E1-20).

    В «Площадках» этого счётчика нет: `show_full_result_count = False`.
    """

    def test_placements_counter_is_framed(self, admin_client: Client, both: dict[str, Any]) -> None:
        changelist: ChangeList = admin_client.get(PLACEMENTS, {"q": "their.com"}).context["cl"]
        # Всего размещений четыре, из них наших — два.
        assert Placement.objects.count() == 4
        assert changelist.full_result_count == 2


class TestMemory:
    def test_product_choice_is_remembered(
        self, admin_client: Client, both: dict[str, Any], clideo: Product
    ) -> None:
        admin_client.get(PLACEMENTS, {"product__id__exact": clideo.pk})
        assert _domains(admin_client, PLACEMENTS) == {"both.com"}

    def test_memory_is_kept_per_screen(
        self, admin_client: Client, both: dict[str, Any], clideo: Product
    ) -> None:
        admin_client.get(PLACEMENTS, {"product__id__exact": clideo.pk})
        # «Площадки» о чужом выборе не знают: память у каждого экрана своя.
        assert _domains(admin_client, SITES, list="all") == {"both.com", "our.com", "their.com"}

    def test_memory_is_kept_per_user(
        self, admin_client: Client, admin_user: User, both: dict[str, Any], clideo: Product
    ) -> None:
        admin_client.get(PLACEMENTS, {"product__id__exact": clideo.pk})
        settings = UserSettings.objects.get(user_id=admin_user.pk)
        assert settings.filters == {"placements.placement": {"product__id__exact": str(clideo.pk)}}

    def test_deleted_list_falls_back_to_all(self, admin_client: Client, convertio: Product) -> None:
        site_list = SiteList.objects.create(name="Сентябрь")
        SiteListItem.objects.create(site_list=site_list, site=Site.objects.create(domain="a.com"))
        Site.objects.create(domain="b.com")
        assert _domains(admin_client, SITES, list=str(site_list.pk)) == {"a.com"}
        SiteListItem.objects.filter(site_list=site_list).delete()
        site_list.delete()
        assert _domains(admin_client, SITES) == {"a.com", "b.com"}


def _untouched(site_id: int, product_id: int) -> dict[str, str]:
    """Форма нового размещения, как её шлёт браузер, если ничего не трогали."""
    return {
        "site": str(site_id),
        "product": str(product_id),
        # Статус нового размещения по умолчанию — «В работе» (модель).
        "status": PlacementStatus.IN_WORK,
        "placement_type": "",
        "seller": "",
        "employee": "",
        "collaborator_order_id": "",
        "ordered_at": "",
        "price_paid_cents": "",
        "currency": "EUR",
        "article_url": "",
        "published_at": "",
        "announce_on_homepage": "unknown",
        "clicks_from_homepage": "",
        "comment": "",
        "links-TOTAL_FORMS": "0",
        "links-INITIAL_FORMS": "0",
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
