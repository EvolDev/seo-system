"""Общие значения GRAY_TERMS и GRAY_ZONES — проверка серости площадки (E2-06).

Условия запроса — запрос пользователя из ручной проверки (04.10.2026) слово в
слово: `site:домен "casino" OR "poker" OR …`. Форекс и знакомства в него не
входят (Q13): условие дописывают в «Настройках». Зоны — 04-DOMAIN-RULES.md
§1.2: меньше 10% — зелёная, 10–25% — жёлтая, больше 25% — красная.
Схему базы миграция не меняет — только строки `domain_settings`.
"""

from typing import Any

from django.db import migrations

SETTINGS = {
    "GRAY_TERMS": (
        [
            "casino",
            "poker",
            "betting",
            "bookmaker",
            "roulette",
            "blackjack",
            "slots",
            "jackpot",
            "gambling",
            "spins",
            "porn",
            "escort",
            "nude",
            "sex",
            "cbd",
            "weed",
            "marijuana",
            "cannabis",
            "hemp",
            "delta 8",
            "delta 9",
            "loan",
            "payday",
            "essay",
            "viagra",
            "cialis",
            "levitra",
            "ed pills",
            "xanax",
        ],
        "Условия второй кнопки «Google: серые темы» в карточке площадки — через OR,"
        " каждое в кавычках.",
    ),
    "GRAY_ZONES": (
        {"green": 10, "yellow": 25},
        "Доля серых страниц, %: меньше green — зелёная зона, до yellow включительно —"
        " жёлтая, больше — красная.",
    ),
}


def add_settings(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    for key, (value, description) in SETTINGS.items():
        DomainSetting.objects.get_or_create(
            key=key, product=None, defaults={"value": value, "description": description}
        )


def remove_settings(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    DomainSetting.objects.filter(key__in=SETTINGS, product=None).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0005_upload_price_cap"),
    ]

    operations = [
        migrations.RunPython(add_settings, remove_settings),
    ]
