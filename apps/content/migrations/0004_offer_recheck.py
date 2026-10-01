"""Общее значение OFFER_RECHECK — порог повторного разбора предложений (E1-08).

Предложение другого продавца, которое человек уже оставил, при новой
загрузке снова ждёт решения, только если цена изменилась больше чем на
3%: цены Collaborator в евро плывут с курсом гривны (ADR-044). Схему базы
миграция не меняет — только строку `domain_settings`.
"""

from typing import Any

from django.db import migrations

KEY = "OFFER_RECHECK"
VALUE = {"min_change_pct": 3}
DESCRIPTION = (
    "Предложение продавца, которое уже оставили, снова ждёт решения, только если"
    " его цена изменилась больше чем на столько процентов."
)


def add_setting(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    DomainSetting.objects.get_or_create(
        key=KEY, product=None, defaults={"value": VALUE, "description": DESCRIPTION}
    )


def remove_setting(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    DomainSetting.objects.filter(key=KEY, product=None).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0003_indexation_schedule"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
