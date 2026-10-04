"""Общее значение UPLOAD_PRICE_CAP — порог «цена из файла похожа на ошибку» (E1-09).

В файле размещений Clideo у 17 строк из 470 цена в тысячу раз больше обычной
(`241 258` вместо €241,26: потерялся десятичный разделитель). Обычные цены — до
€568. Цена выше порога не записывается, строка — в сводку до записи (ADR-051).
Схему базы миграция не меняет — только строку `domain_settings`.
"""

from typing import Any

from django.db import migrations

KEY = "UPLOAD_PRICE_CAP"
VALUE = {"eur": 5000}
DESCRIPTION = (
    "Цена из файла выше этой суммы в евро похожа на ошибку: не записывается,"
    " строка попадает в сводку до записи."
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
        ("content", "0004_offer_recheck"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
