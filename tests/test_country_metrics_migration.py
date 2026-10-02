"""Перенос US Traff в трафик по странам — миграция `sites.0014` (E1-10, ADR-045).

US Traff из таблицы — трафик США у каждой площадки, а не топ-регион: он
переезжает снимком страны `us` с той же датой и источником, колонка
`site_metrics.us_traffic` уходит. Замеры со слов продавца не переносятся —
трафик по странам у нас всегда свой.

Миграции гоняются по-настоящему, как в `test_offers_migration.py`.
"""

import datetime as dt
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("sites", "0013_ahrefs_batch_enums")]
AFTER = [("sites", "0014_country_metrics")]


def _models(executor: MigrationExecutor, target: list[tuple[str, str]]) -> Any:
    return executor.loader.project_state(target).apps


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_us_traffic_moved_to_country_snapshots() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = _models(executor, BEFORE)
    site_model = apps.get_model("sites", "Site")
    metric_model = apps.get_model("sites", "SiteMetric")
    seller = apps.get_model("sites", "Seller").objects.create(name="Zain")
    day = dt.datetime(2026, 9, 26, tzinfo=dt.UTC)
    a = site_model.objects.create(domain="a.com")
    b = site_model.objects.create(domain="b.com")
    metric_model.objects.create(
        site=a, dr=50, us_traffic=1200, top_geo="in", source="csv_import", checked_at=day
    )
    metric_model.objects.create(site=b, dr=40, us_traffic=0, source="csv_import", checked_at=day)
    metric_model.objects.create(site=b, dr=41, us_traffic=None, checked_at=day)
    metric_model.objects.create(site=a, dr=60, us_traffic=999, seller=seller, checked_at=day)

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    apps = _models(executor, AFTER)
    country_model = apps.get_model("sites", "SiteCountryMetric")
    moved = sorted(
        country_model.objects.values_list(
            "site__domain", "country", "organic_traffic", "total_keywords", "source", "checked_at"
        )
    )
    assert moved == [
        ("a.com", "us", 1200, None, "csv_import", day),
        ("b.com", "us", 0, None, "csv_import", day),
    ]
    columns = [
        c.name
        for c in connection.introspection.get_table_description(connection.cursor(), "site_metrics")
    ]
    assert "us_traffic" not in columns
    assert apps.get_model("sites", "SiteMetric").objects.count() == 4
