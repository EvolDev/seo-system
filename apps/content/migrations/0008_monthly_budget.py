"""Общее значение MONTHLY_BUDGET — бюджет на размещения в месяц (экран «Экономика»).

30 000 евро в месяц — сумма, которую выдают на линкбилдинг (пользователь,
14.10.2026); каждый месяц начинается заново. Схему базы миграция не меняет.
"""

from typing import Any

from django.db import migrations

KEY = "MONTHLY_BUDGET"
VALUE = {"eur": 30000}
DESCRIPTION = (
    "Бюджет на размещения в месяц, евро: каждый месяц начинается с полной суммы."
    " Продукт может задать свою сумму."
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
        ("content", "0007_anchor_recommend"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
