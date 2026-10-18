# Копирайтеры (ADR-072): наши авторы статей со ставкой за статью. Имя уникально
# без учёта регистра — как у продавцов.

import django.db.models.functions.text
from django.db import migrations, models

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0008_monthly_budget"),
    ]

    operations = [
        migrations.CreateModel(
            name="Copywriter",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.TextField(verbose_name="имя")),
                (
                    "rate_cents",
                    models.IntegerField(
                        blank=True, null=True, verbose_name="ставка за статью, центы"
                    ),
                ),
                (
                    "currency",
                    models.CharField(
                        db_default="EUR", default="EUR", max_length=3, verbose_name="валюта ставки"
                    ),
                ),
                ("contacts", models.TextField(blank=True, null=True, verbose_name="контакты")),
                ("notes", models.TextField(blank=True, null=True, verbose_name="заметки")),
                (
                    "is_active",
                    models.BooleanField(
                        db_default=True,
                        default=True,
                        help_text="Не работает — не предлагается в размещении, его статьи остаются.",
                        verbose_name="работает",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="заведён"),
                ),
            ],
            options={
                "verbose_name": "копирайтер",
                "verbose_name_plural": "копирайтеры",
                "db_table": "copywriters",
                "constraints": [
                    models.UniqueConstraint(
                        django.db.models.functions.text.Lower("name"),
                        name="copywriters_name_key",
                        violation_error_message="Копирайтер с таким именем уже есть (регистр не важен).",
                    )
                ],
            },
        ),
    ]
