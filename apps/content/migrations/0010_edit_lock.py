"""Общее значение EDIT_LOCK — через сколько минут простоя блокировка правки снимается.

Пользователь (28.10.2026): площадку, которую правит один, второй пока не меняет.
Ушёл, не закрыв панель, — через 15 минут без правок площадка снова свободна.
Схему базы миграция не меняет.
"""

from typing import Any

from django.db import migrations

KEY = "EDIT_LOCK"
VALUE = {"idle_minutes": 15}
DESCRIPTION = (
    "Блокировка правки площадки и размещений: через сколько минут без правок она"
    " снимается сама, если панель не закрыли."
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
        ("content", "0009_copywriters"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
