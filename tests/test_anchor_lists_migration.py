"""Списки анкоров (ADR-075): нынешние анкоры — список «США», размещения с анкорами — в нём;
откат возвращает уникальность анкора внутри продукта и представление."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("keywords", "0005_anchor_activity"), ("placements", "0011_price")]
AFTER = [("keywords", "0007_anchor_list_required"), ("placements", "0012_anchor_list")]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_anchor_lists_migration_and_reverse() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    products = apps.get_model("sites", "Product")
    keywords = apps.get_model("keywords", "Keyword")
    placements = apps.get_model("placements", "Placement")
    links = apps.get_model("placements", "PlacementLink")
    sites = apps.get_model("sites", "Site")
    convertio = products.objects.create(name="Convertio", domain="convertio.co")
    clideo = products.objects.create(name="Clideo", domain="clideo.com")
    convert = keywords.objects.create(
        product=convertio, keyword="convert", target_url="https://convertio.co/"
    )
    keywords.objects.create(product=convertio, keyword="mp4 to mp3", target_url="https://x/")
    linked = placements.objects.create(
        site=sites.objects.create(domain="a.com"), product=convertio, status="placed"
    )
    links.objects.create(
        placement=linked, keyword=convert, anchor="convert", target_url="https://convertio.co/"
    )
    empty = placements.objects.create(
        site=sites.objects.create(domain="dci.com.br"), product=convertio, status="in_work"
    )
    loose = placements.objects.create(
        site=sites.objects.create(domain="b.com"), product=clideo, status="placed"
    )
    links.objects.create(placement=loose, anchor="video editor", target_url="https://clideo.com/")

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    apps = executor.loader.project_state(AFTER).apps
    lists = apps.get_model("keywords", "AnchorList")
    keywords = apps.get_model("keywords", "Keyword")
    placements = apps.get_model("placements", "Placement")
    us = lists.objects.get()
    assert (us.product_id, us.country) == (convertio.pk, "us")
    assert set(keywords.objects.values_list("anchor_list_id", flat=True)) == {us.pk}
    assert placements.objects.get(pk=linked.pk).anchor_list_id == us.pk
    # Без ссылок — не выбирали: угадается при открытии, бразильская встанет на Бразилию.
    assert placements.objects.get(pk=empty.pk).anchor_list_id is None
    assert placements.objects.get(pk=loose.pk).anchor_list_id is None
    with connection.cursor() as cursor:
        cursor.execute("SELECT anchor_list_id FROM v_keyword_coverage WHERE id = %s", [convert.pk])
        assert cursor.fetchone() == (us.pk,)

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM v_keyword_coverage")
        assert cursor.fetchone() == (2,)
        cursor.execute("SELECT to_regclass('anchor_lists')")
        assert cursor.fetchone() == (None,)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
