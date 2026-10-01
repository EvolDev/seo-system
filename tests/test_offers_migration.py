"""Перенос данных к рабочей цене — миграция `sites.0007` (E1-07, ADR-043).

Критерий приёмки: после миграции цены из таблицы — предложения Collaborator
и рабочие цены площадок, суммы сходятся со снапшотами до миграции; заметки
и причины отказа — в истории с источником «таблица линкбилдинга».

Миграции гоняются по-настоящему, каждая в своей транзакции: перенос данных и
следующая за ним правка таблицы в одной транзакции Postgres не пускает.
`serialized_rollback` возвращает после теста данные, которые миграции
заводят сами (Collaborator, настройки), — их ждут другие тесты.
"""

import datetime as dt
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum

BEFORE = [("sites", "0006_sellers_offers_notes_rates")]
AFTER = [("sites", "0007_offers_data")]


def _models(executor: MigrationExecutor, target: list[tuple[str, str]]) -> Any:
    return executor.loader.project_state(target).apps


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_prices_notes_and_reasons_moved() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = _models(executor, BEFORE)
    product = apps.get_model("sites", "Product").objects.create(
        name="Convertio", domain="convertio.co"
    )
    site_model = apps.get_model("sites", "Site")
    price_model = apps.get_model("sites", "SitePrice")
    noted = site_model.objects.create(domain="noted.com", notes="Отвечают быстро")
    rejected = site_model.objects.create(domain="rejected.com")
    apps.get_model("sites", "ProductSite").objects.create(
        product=product, site=rejected, status="rejected", reject_reason="Nofollow, отбрасываем"
    )
    early = dt.datetime(2026, 8, 27, tzinfo=dt.UTC)
    late = dt.datetime(2026, 9, 27, tzinfo=dt.UTC)
    price_model.objects.create(site=noted, placement_cents=20000, checked_at=early)
    last = price_model.objects.create(
        site=noted, placement_cents=24000, announce_cents=0, writing_cents=1500, checked_at=late
    )
    price_model.objects.create(site=rejected, placement_cents=9500, checked_at=late)
    fields = ("placement_cents", "announce_cents", "writing_cents")
    sums = price_model.objects.aggregate(*(Sum(field) for field in fields))

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    apps = _models(executor, AFTER)
    price_model = apps.get_model("sites", "SitePrice")
    collaborator = apps.get_model("sites", "Seller").objects.get(is_collaborator=True)
    assert price_model.objects.count() == 3
    assert price_model.objects.aggregate(*(Sum(field) for field in fields)) == sums
    assert set(price_model.objects.values_list("seller_id", flat=True)) == {collaborator.pk}
    assert set(price_model.objects.values_list("placement_type", flat=True)) == {"guest_post"}
    assert not price_model.objects.filter(reviewed_at__isnull=True).exists()
    sites = apps.get_model("sites", "Site").objects
    assert sites.get(domain="noted.com").price_id == last.pk
    notes = {
        (note.site.domain, note.product_id, note.body, note.source)
        for note in apps.get_model("sites", "SiteNote").objects.select_related("site")
    }
    assert notes == {
        ("noted.com", None, "Отвечают быстро", "таблица линкбилдинга"),
        ("rejected.com", product.pk, "Nofollow, отбрасываем", "таблица линкбилдинга"),
    }

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
