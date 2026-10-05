"""Общее значение ANCHOR_RECOMMEND — рекомендации анкоров в размещении (E3-05, ADR-059).

Ключи в топ-1–3 не предлагаются (04-DOMAIN-RULES.md §2.2), в списке — 10
анкоров (просьба пользователя 05.10.2026). Схему базы миграция не меняет.
"""

from typing import Any

from django.db import migrations

KEY = "ANCHOR_RECOMMEND"
VALUE = {"skip_top": 3, "limit": 10}
DESCRIPTION = (
    "Рекомендации анкоров в размещении: ключ на позиции от 1 до skip_top не предлагается,"
    " limit — сколько анкоров в списке."
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
        ("content", "0006_gray_settings"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
