"""Новые статусы площадки — миграция `sites.0015` (E1-12, ADR-047).

Критерии приёмки: «Отклонена» становится «Отбрасываю», причины на месте;
площадка с заявкой в работе — «Заявка отправлена», с публикацией —
«Размещались», но прежний отказ, чёрный список и аудит догоняющий проход не
трогает. Значения типа — в порядке окна статуса. Откат возвращает старые
значения.

Миграции гоняются по-настоящему, как в `test_offers_migration.py`.
"""

from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("sites", "0014_country_metrics")]
AFTER = [("sites", "0015_site_statuses")]
REASON = "Nofollow, отбрасываем"


def _models(executor: MigrationExecutor, target: list[tuple[str, str]]) -> Any:
    return executor.loader.project_state(target).apps


def _enum() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'site_status' ORDER BY e.enumsortorder"
        )
        return [label for (label,) in cursor.fetchall()]


def _statuses(apps: Any) -> dict[tuple[str, str], tuple[str, str | None, bool]]:
    rows = apps.get_model("sites", "ProductSite").objects.select_related("site", "product")
    return {
        (row.site.domain, row.product.name): (row.status, row.reject_reason, row.imported_undecided)
        for row in rows
    }


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_statuses_converted_and_caught_up() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    apps = _models(executor, BEFORE)
    product_model = apps.get_model("sites", "Product")
    convertio = product_model.objects.create(name="Convertio", domain="convertio.co")
    clideo = product_model.objects.create(name="Clideo", domain="clideo.com")
    site_model = apps.get_model("sites", "Site")
    decision = apps.get_model("sites", "ProductSite").objects
    placement = apps.get_model("placements", "Placement").objects
    # (статус Convertio, причина, пометка «без решения», статус размещения Convertio)
    cases: dict[str, tuple[str, str | None, bool, str | None]] = {
        "rejected.com": ("rejected", REASON, False, None),
        "ordered.com": ("approved", None, False, "ordered"),
        "planned.com": ("approved", None, False, "planned"),
        "writing.com": ("new", None, True, "writing"),
        "published.com": ("approved", None, False, "published"),
        "rejected-published.com": ("rejected", REASON, False, "published"),
        "black.com": ("blacklisted", None, False, "ordered"),
        "audit.com": ("auditing", None, False, "review"),
    }
    for domain, (status, reason, undecided, placed) in cases.items():
        site = site_model.objects.create(domain=domain)
        for product in (convertio, clideo):
            decision.create(site=site, product=product)
        decision.filter(site=site, product=convertio).update(
            status=status, reject_reason=reason, imported_undecided=undecided
        )
        if placed:
            placement.create(site=site, product=convertio, status=placed)
    # Публикация Clideo двигает только строку Clideo.
    placement.create(
        site=site_model.objects.get(domain="planned.com"), product=clideo, status="published"
    )

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    assert _enum() == [
        "new",
        "viewed",
        "approved",
        "ordered",
        "placed",
        "discarded",
        "declined",
        "blacklisted",
        "auditing",
    ]
    after = _statuses(_models(executor, AFTER))
    assert {key: value for key, value in after.items() if key[1] == "Convertio"} == {
        ("rejected.com", "Convertio"): ("discarded", REASON, False),
        ("ordered.com", "Convertio"): ("ordered", None, False),
        ("planned.com", "Convertio"): ("approved", None, False),
        ("writing.com", "Convertio"): ("ordered", None, False),
        ("published.com", "Convertio"): ("placed", None, False),
        ("rejected-published.com", "Convertio"): ("discarded", REASON, False),
        ("black.com", "Convertio"): ("blacklisted", None, False),
        ("audit.com", "Convertio"): ("auditing", None, False),
    }
    assert after[("planned.com", "Clideo")] == ("placed", None, False)
    assert after[("ordered.com", "Clideo")] == ("new", None, False)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM v_product_site_latest")
        assert cursor.fetchone() == (16,)

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    assert _enum() == ["new", "auditing", "approved", "rejected", "placed", "blacklisted"]
    back = _statuses(_models(executor, BEFORE))
    assert back[("rejected.com", "Convertio")] == ("rejected", REASON, False)
    assert back[("ordered.com", "Convertio")] == ("approved", None, False)

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
