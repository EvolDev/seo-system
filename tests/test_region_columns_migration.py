"""Раскладки выгрузки (ADR-076): «US Traff» → «Region Traff», после неё — «Region»;
колонка со своим заголовком не меняется; откат возвращает как было."""

from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("workspace", "0005_export_profiles")]
AFTER = [("workspace", "0006_region_columns")]


def _column(n: int, source: str, header: str) -> dict[str, Any]:
    return {
        "id": f"c{n}",
        "index": None,
        "header": header,
        "source": source,
        "enabled": True,
        "constant": "",
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_region_columns_migration_and_reverse() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    user = apps.get_model("auth", "User").objects.create(username="alice")
    profiles = apps.get_model("workspace", "ExportProfile")
    base = {"version": 1, "mode": "custom", "order": "current"}
    mine = profiles.objects.create(
        user=user,
        screen="placements.placement",
        name="Список для барыг",
        config={
            **base,
            "region": "br",
            "columns": [
                _column(0, "target", "Site URL"),
                _column(1, "us_traffic", "US Traff"),
                _column(2, "dr", "DR"),
            ],
        },
    )
    own = profiles.objects.create(
        user=user,
        screen="placements.placement",
        name="Свой заголовок",
        config={
            **base,
            "columns": [_column(0, "us_traffic", "Трафик США"), _column(1, "region", "Регион")],
        },
    )

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    apps = executor.loader.project_state(AFTER).apps
    profiles = apps.get_model("workspace", "ExportProfile")
    config = profiles.objects.get(pk=mine.pk).config
    assert "region" not in config
    assert [(c["source"], c["header"]) for c in config["columns"]] == [
        ("target", "Site URL"),
        ("region_traffic", "Region Traff"),
        ("region", "Region"),
        ("dr", "DR"),
    ]
    assert profiles.objects.get(pk=mine.pk).revision == 2
    kept = profiles.objects.get(pk=own.pk)
    assert [(c["source"], c["header"]) for c in kept.config["columns"]] == [
        ("us_traffic", "Трафик США"),
        ("region", "Регион"),
    ]
    assert kept.revision == 1

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    back = apps.get_model("workspace", "ExportProfile").objects.get(pk=mine.pk).config
    assert [(c["source"], c["header"]) for c in back["columns"]] == [
        ("target", "Site URL"),
        ("us_traffic", "US Traff"),
        ("dr", "DR"),
    ]
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
