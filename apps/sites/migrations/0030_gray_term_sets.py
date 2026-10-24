# Наборы условий серости по языкам (ADR-074) вместо общей настройки GRAY_TERMS.
# Английский — значение GRAY_TERMS как есть (его могли дописать руками),
# португальский — список пользователя 24.10.2026, сокращённый до лимита Google
# в 32 слова вместе с `site:домен`. Настройка GRAY_TERMS уходит; откат
# возвращает её из английского набора.

from typing import Any

import django.contrib.postgres.fields
from django.db import migrations, models

import config.db

# Стартовый английский — если GRAY_TERMS стёрли руками: как в content.0006.
ENGLISH = [
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
]
# Фразы, в которых уже есть слово из списка («apostas esportivas» — «apostas»),
# убраны: запрос со словом находит и их.
PORTUGUESE = [
    "casino",
    "cassino",
    "poker",
    "pôquer",
    "apostas",
    "roleta",
    "blackjack",
    "caça-níqueis",
    "jackpot",
    "jogos de azar",
    "porno",
    "pornografia",
    "acompanhantes",
    "prostituição",
    "sexo",
    "CBD",
    "canabidiol",
    "maconha",
    "cannabis",
    "marijuana",
    "empréstimo",
    "empréstimos",
    "comprar TCC",
    "viagra",
    "cialis",
    "levitra",
    "xanax",
    "alprazolam",
]
GRAY_TERMS_DESCRIPTION = (
    "Условия второй кнопки «Google: серые темы» в карточке площадки — через OR, каждое в кавычках."
)


def move_terms(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    GrayTermSet = apps.get_model("sites", "GrayTermSet")
    setting = DomainSetting.objects.filter(key="GRAY_TERMS", product=None).first()
    english = setting.value if setting is not None and setting.value else ENGLISH
    GrayTermSet.objects.create(language="en", terms=english)
    GrayTermSet.objects.create(language="pt", terms=PORTUGUESE)
    DomainSetting.objects.filter(key="GRAY_TERMS").delete()


def restore_setting(apps: Any, schema_editor: Any) -> None:
    DomainSetting = apps.get_model("content", "DomainSetting")
    GrayTermSet = apps.get_model("sites", "GrayTermSet")
    english = GrayTermSet.objects.filter(language="en").first()
    DomainSetting.objects.get_or_create(
        key="GRAY_TERMS",
        product=None,
        defaults={
            "value": english.terms if english is not None else ENGLISH,
            "description": GRAY_TERMS_DESCRIPTION,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0009_copywriters"),
        ("sites", "0029_upload_extra_prices"),
    ]

    operations = [
        migrations.CreateModel(
            name="GrayTermSet",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("language", models.TextField(verbose_name="язык")),
                (
                    "terms",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.TextField(), size=None, verbose_name="условия"
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="изменён"),
                ),
            ],
            options={
                "verbose_name": "набор условий серости",
                "verbose_name_plural": "наборы условий серости",
                "db_table": "gray_term_sets",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("language",),
                        name="gray_term_sets_language_key",
                        violation_error_message="Набор для этого языка уже есть.",
                    )
                ],
            },
        ),
        migrations.RunPython(move_terms, restore_setting),
    ]
