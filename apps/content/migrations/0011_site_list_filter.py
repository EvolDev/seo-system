"""Общее значение SITE_LIST_FILTER — сколько последних списков в фильтре «Список».

Пользователь (09.11.2026): каждая загрузка заводит рабочий список, и выпадающий
фильтр в «Площадках» растёт без конца, а нужны обычно один-два последних;
загружать будут несколько человек — показывать последние 10. Схему базы миграция
не меняет.
"""

from typing import Any

from django.db import migrations

KEY = "SITE_LIST_FILTER"
VALUE = {"recent": 10}
DESCRIPTION = (
    "Фильтр «Список» в «Площадках»: сколько последних рабочих списков в нём"
    " показывать. Остальные — «Справочники → Рабочие списки»."
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
        ("content", "0010_edit_lock"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
