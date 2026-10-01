"""Общее значение INDEXATION_SCHEDULE — сроки проверки индексации (E2-03).

Сроки — из `04-DOMAIN-RULES.md` §4. Расписание заводится выключенным:
первый прогон проверяет все опубликованные статьи разом, и включает его
человек, когда готов. Схему базы миграция не меняет — только строку
`domain_settings`.
"""

from typing import Any

from django.db import migrations

KEY = "INDEXATION_SCHEDULE"
VALUE = {
    "enabled": False,
    "first_check_days": 3,
    "retry_days": 1,
    "recheck_days": 30,
    "alert_after_days": 30,
}
DESCRIPTION = (
    "Проверка индексации: по расписанию или нет; через сколько дней после публикации"
    " первая проверка; как часто повторять, пока статьи нет в индексе; как часто"
    " перепроверять статью в индексе; через сколько дней без индекса оповещать."
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
        ("content", "0002_domain_settings"),
    ]

    operations = [
        migrations.RunPython(add_setting, remove_setting),
    ]
