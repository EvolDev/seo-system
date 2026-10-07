"""Оценка площадки звёздочкой (E1-21, ADR-064).

Оценка общая для площадки, одна строка на человека: передумал — строка
перезаписывается. Среднее берётся по всем людям и считается в
`v_site_latest`, поэтому проверяем его через сам список, а не только
через запись.
"""

import pytest
from django.contrib.auth.models import Permission, User
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site, SiteRating

pytestmark = pytest.mark.django_db

SITES = reverse("admin:sites_productsitelatest_changelist")
PLACEMENTS = reverse("admin:placements_placement_changelist")


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def site(product: Product) -> Site:
    # Строку «продукт × площадка» система заводит сама на каждую пару (ADR-030),
    # руками её создавать не нужно.
    return Site.objects.create(domain="donor.com")


def rate_url(site: Site) -> str:
    return reverse("admin:sites_site_rate", args=[site.pk])


def colleague(name: str = "colleague") -> User:
    """Второй человек с правом менять площадку — для среднего по нескольким."""
    user = User.objects.create_user(name, password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="change_site"))
    return user


class TestRecording:
    def test_rating_is_saved_and_returned(self, admin_client: Client, site: Site) -> None:
        response = admin_client.post(rate_url(site), {"value": "4"})
        assert response.status_code == 200
        assert response.json() == {
            "average": "4.0",
            "count": 1,
            "mine": 4,
            "title": "4.0 · оценок: 1 · ваша 4",
        }

    def test_second_rating_replaces_the_first(self, admin_client: Client, site: Site) -> None:
        """Человек передумал — строка одна, история не копится (решение 07.10.2026)."""
        admin_client.post(rate_url(site), {"value": "2"})
        admin_client.post(rate_url(site), {"value": "5"})
        assert SiteRating.objects.filter(site=site).count() == 1
        assert SiteRating.objects.get(site=site).value == 5

    def test_average_counts_every_person(self, admin_client: Client, site: Site) -> None:
        admin_client.post(rate_url(site), {"value": "5"})
        other = Client()
        other.force_login(colleague())
        response = other.post(rate_url(site), {"value": "2"})
        # (5 + 2) / 2 = 3.5 — среднее по всем людям, а не последняя оценка.
        assert response.json()["average"] == "3.5"
        assert response.json()["count"] == 2

    def test_empty_value_removes_the_rating(self, admin_client: Client, site: Site) -> None:
        admin_client.post(rate_url(site), {"value": "3"})
        response = admin_client.post(rate_url(site), {"value": ""})
        assert not SiteRating.objects.filter(site=site).exists()
        assert response.json() == {
            "average": None,
            "count": 0,
            "mine": None,
            "title": "Оценить площадку",
        }

    @pytest.mark.parametrize("bad", ["0", "6", "-1", "пять"])
    def test_value_out_of_range_is_refused(
        self, admin_client: Client, site: Site, bad: str
    ) -> None:
        """Мусор — отказ, а не снятие: опечатка не должна стирать оценку."""
        admin_client.post(rate_url(site), {"value": "4"})
        response = admin_client.post(rate_url(site), {"value": bad})
        assert response.status_code == 400
        assert SiteRating.objects.get(site=site).value == 4

    def test_get_is_not_allowed(self, admin_client: Client, site: Site) -> None:
        assert admin_client.get(rate_url(site)).status_code == 405

    def test_without_permission_nothing_is_written(self, site: Site) -> None:
        looker = User.objects.create_user("looker", password="x", is_staff=True)
        looker.user_permissions.add(Permission.objects.get(codename="view_site"))
        client = Client()
        client.force_login(looker)
        assert client.post(rate_url(site), {"value": "5"}).status_code == 403
        assert not SiteRating.objects.exists()


class TestColumns:
    def test_star_and_number_stand_before_the_domain(
        self, admin_client: Client, site: Site
    ) -> None:
        admin_client.post(rate_url(site), {"value": "4"})
        page = admin_client.get(SITES).content.decode()
        star = page.index('class="seo-rating"')
        domain = page.index("donor.com", star)
        assert star < domain
        assert '<span class="seo-rating-value">4.0</span>' in page

    def test_site_without_ratings_shows_no_number(self, admin_client: Client, site: Site) -> None:
        """«0.0» читалось бы как плохая площадка, а это «никто не оценивал»."""
        page = admin_client.get(SITES).content.decode()
        assert "seo-rating-empty" in page
        assert 'class="seo-rating-value"' not in page

    def test_placements_show_the_same_rating(
        self, admin_client: Client, site: Site, product: Product
    ) -> None:
        Placement.objects.create(site=site, product=product, status=PlacementStatus.IN_WORK)
        admin_client.post(rate_url(site), {"value": "3"})
        page = admin_client.get(PLACEMENTS).content.decode()
        assert '<span class="seo-rating-value">3.0</span>' in page

    def test_own_rating_rides_along_for_the_picker(self, admin_client: Client, site: Site) -> None:
        """`data-mine` — чтобы окошко подсветило уже поставленную оценку."""
        admin_client.post(rate_url(site), {"value": "2"})
        page = admin_client.get(SITES).content.decode()
        assert 'data-mine="2"' in page

    def test_other_people_rating_is_not_mine(self, admin_client: Client, site: Site) -> None:
        other = Client()
        other.force_login(colleague())
        other.post(rate_url(site), {"value": "5"})
        page = admin_client.get(SITES).content.decode()
        assert '<span class="seo-rating-value">5.0</span>' in page
        assert 'data-mine=""' in page


class TestQueries:
    def test_list_cost_does_not_grow_with_rows(
        self, admin_client: Client, product: Product
    ) -> None:
        """Среднее идёт из представления: десять площадок стоят столько же, сколько одна.

        Подзапрос на строку здесь недопустим — в каталоге 45 000 площадок,
        на заметках это уже обжигало (E1-08).
        """
        _add_sites(1)
        admin_client.get(SITES)  # прогрев: первый заход тянет настройки и права
        one = _count_queries(admin_client)
        _add_sites(10, start=1)
        many = _count_queries(admin_client)
        assert one == many


def _add_sites(how_many: int, start: int = 0) -> None:
    for number in range(start, start + how_many):
        Site.objects.create(domain=f"donor{number}.com")


def _count_queries(client: Client) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        client.get(SITES)
    return len(captured)
