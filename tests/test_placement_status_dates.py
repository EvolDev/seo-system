"""Даты статусов размещения: «Заявка отправлена» и «Размещено» (E1-22).

Дату отмечает `Placement.save()`, а не скрипт формы: статус меняют ещё
массовым действием и импортом, и там скрипта нет. Заполненную дату код не
трогает, назад не стирает.
"""

import datetime as dt

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = pytest.mark.django_db

PLACEMENTS = reverse("admin:placements_placement_changelist")


@pytest.fixture
def product() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def site() -> Site:
    return Site.objects.create(domain="donor.com")


@pytest.fixture
def placement(site: Site, product: Product) -> Placement:
    return Placement.objects.create(site=site, product=product, status=PlacementStatus.IN_WORK)


class TestStamping:
    def test_ordered_status_stamps_the_sent_date(self, placement: Placement) -> None:
        assert placement.ordered_at is None
        placement.status = PlacementStatus.ORDERED
        placement.save()
        placement.refresh_from_db()
        assert placement.ordered_at is not None
        assert timezone.localtime(placement.ordered_at).date() == timezone.localdate()

    def test_placed_status_stamps_the_publication_date(self, placement: Placement) -> None:
        placement.status = PlacementStatus.PLACED
        placement.save()
        placement.refresh_from_db()
        assert placement.published_at is not None

    def test_own_date_is_not_overwritten(self, placement: Placement) -> None:
        """Дата из файла или поставленная руками важнее сегодняшнего дня."""
        mine = timezone.make_aware(dt.datetime(2026, 10, 5, 12, 0))
        placement.ordered_at = mine
        placement.status = PlacementStatus.ORDERED
        placement.save()
        placement.refresh_from_db()
        assert placement.ordered_at == mine

    def test_date_survives_the_next_status(self, placement: Placement) -> None:
        """Ушли дальше по лесенке — заявка всё равно была отправлена тогда-то."""
        placement.status = PlacementStatus.ORDERED
        placement.save()
        sent = placement.ordered_at
        placement.status = PlacementStatus.WRITING
        placement.save()
        placement.refresh_from_db()
        assert placement.ordered_at == sent

    def test_statuses_without_a_date_stamp_nothing(self, placement: Placement) -> None:
        placement.status = PlacementStatus.WRITING
        placement.save()
        placement.refresh_from_db()
        assert placement.ordered_at is None
        assert placement.published_at is None

    def test_new_record_is_not_stamped(self, site: Site, product: Product) -> None:
        """Импорт заводит запись со статусом и датой из файла.

        Сегодняшний день вместо пустой даты был бы выдумкой: статья по файлу
        могла выйти месяцы назад. Отмечаем только смену статуса, не создание.
        """
        made = Placement.objects.create(site=site, product=product, status=PlacementStatus.PLACED)
        made.refresh_from_db()
        assert made.published_at is None

    def test_saving_without_changing_status_stamps_nothing(self, placement: Placement) -> None:
        """Правка комментария не должна выглядеть как смена статуса."""
        placement.status = PlacementStatus.ORDERED
        placement.save()
        first = placement.ordered_at
        placement.ordered_at = None
        placement.comment = "позвонить"
        placement.save()
        placement.refresh_from_db()
        # Статус тот же — дату не выставляем заново, даже если её стёрли руками.
        assert placement.ordered_at is None
        assert first is not None

    def test_update_fields_does_not_lose_the_date(self, placement: Placement) -> None:
        """Массовое действие пишет выборочно — дату нужно дописать в update_fields."""
        placement.status = PlacementStatus.ORDERED
        placement.save(update_fields=["status", "updated_at"])
        placement.refresh_from_db()
        assert placement.ordered_at is not None


class TestBulkAction:
    def test_action_stamps_the_date(self, admin_client: Client, placement: Placement) -> None:
        admin_client.post(
            PLACEMENTS,
            {
                "action": "set_status_action",
                "status": PlacementStatus.ORDERED,
                "_selected_action": [str(placement.pk)],
            },
        )
        placement.refresh_from_db()
        assert placement.status == PlacementStatus.ORDERED
        assert placement.ordered_at is not None


class TestScreens:
    def test_list_shows_the_sent_date_column(
        self, admin_client: Client, placement: Placement
    ) -> None:
        placement.status = PlacementStatus.ORDERED
        placement.save()
        page = admin_client.get(PLACEMENTS).content.decode()
        assert "отправлена" in page
        assert f"{timezone.localdate():%d.%m.%Y}" in page

    def test_card_shows_only_the_date_of_the_current_status(
        self, admin_client: Client, placement: Placement
    ) -> None:
        """Обе даты есть в базе, но под статусом видна одна — та, что к нему."""
        placement.ordered_at = timezone.make_aware(dt.datetime(2026, 10, 5, 12, 0))
        placement.published_at = timezone.make_aware(dt.datetime(2026, 10, 6, 12, 0))
        placement.status = PlacementStatus.ORDERED
        placement.save()
        card = reverse("admin:placements_placement_change", args=[placement.pk])
        page = admin_client.get(card).content.decode()
        line = page[page.index("field-status_date") : page.index("field-status_history")]
        assert "05.10.2026" in line
        assert "06.10.2026" not in line

    def test_card_without_a_status_date_shows_a_dash(
        self, admin_client: Client, placement: Placement
    ) -> None:
        card = reverse("admin:placements_placement_change", args=[placement.pk])
        page = admin_client.get(card).content.decode()
        line = page[page.index("field-status_date") : page.index("field-status_history")]
        assert "—" in line
