"""Тип каталога переносится только своему продавцу; откат сохраняет представления."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("sites", "0026_seller_ratings"), ("placements", "0008_work_status")]
AFTER = [("sites", "0027_offer_link_type"), ("placements", "0009_link_type")]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_link_type_migration_and_reverse() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = executor.loader.project_state(BEFORE).apps
    sites = apps.get_model("sites", "Site")
    prices = apps.get_model("sites", "SitePrice")
    placements = apps.get_model("placements", "Placement")
    collaborator = apps.get_model("sites", "Seller").objects.get(is_collaborator=True)
    other = apps.get_model("sites", "Seller").objects.create(name="Different seller")
    product = apps.get_model("sites", "Product").objects.create(
        name="Convertio", domain="convertio.co"
    )
    site = sites.objects.create(
        domain="catalog.example", source="Collaborator", link_type="dofollow"
    )
    working = prices.objects.create(site=site, seller=collaborator, placement_cents=10000)
    other_price = prices.objects.create(site=site, seller=other, placement_cents=12000)
    insertion = prices.objects.create(
        site=site, seller=collaborator, placement_type="link_insertion", placement_cents=8000
    )
    sites.objects.filter(pk=site.pk).update(price_id=working.pk)
    unknown = sites.objects.create(domain="vendor.example", source=other.name, link_type="nofollow")
    unproven = prices.objects.create(site=unknown, seller=collaborator, placement_cents=9000)
    order = placements.objects.create(
        site=site,
        product=product,
        seller=collaborator,
        placement_type="guest_post",
        status="in_work",
    )
    different = placements.objects.create(
        site=site, product=product, seller=other, placement_type="guest_post", status="in_work"
    )
    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    apps = executor.loader.project_state(AFTER).apps
    prices = apps.get_model("sites", "SitePrice")
    assert prices.objects.get(pk=working.pk).link_type == "dofollow"
    assert not prices.objects.filter(
        pk__in=[other_price.pk, insertion.pk, unproven.pk], link_type__isnull=False
    ).exists()
    placements = apps.get_model("placements", "Placement")
    assert placements.objects.get(pk=order.pk).link_type == "dofollow"
    assert placements.objects.get(pk=different.pk).link_type is None

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('v_site_offers'), to_regclass('v_site_latest'), "
            "to_regclass('v_product_site_latest')"
        )
        assert all(cursor.fetchone())
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
