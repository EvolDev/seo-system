# Списки анкоров по регионам (ADR-075). Нынешние анкоры — список «США» своего
# продукта (пользователь, 24.10.2026: «те анкоры, которые сейчас мы учитываем, —
# это всё US регион»). Колонка списка у анкора здесь ещё необязательная:
# NOT NULL — в 0007, отдельной транзакцией после заполнения.

from typing import Any

import django.db.models.deletion
from django.db import migrations, models

import config.db


def fill_lists(apps: Any, schema_editor: Any) -> None:
    AnchorList = apps.get_model("keywords", "AnchorList")
    Keyword = apps.get_model("keywords", "Keyword")
    products = Keyword.objects.values_list("product_id", flat=True).distinct().order_by()
    for product_id in sorted(set(products)):
        anchor_list = AnchorList.objects.create(product_id=product_id, country="us")
        Keyword.objects.filter(product_id=product_id).update(anchor_list=anchor_list)


def clear_lists(apps: Any, schema_editor: Any) -> None:
    Keyword = apps.get_model("keywords", "Keyword")
    Keyword.objects.update(anchor_list=None)


class Migration(migrations.Migration):
    dependencies = [
        ("keywords", "0005_anchor_activity"),
        ("sites", "0030_gray_term_sets"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnchorList",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("country", models.CharField(max_length=2, verbose_name="регион")),
                (
                    "created_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="заведён"),
                ),
                (
                    "product",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="anchor_lists",
                        to="sites.product",
                        verbose_name="продукт",
                    ),
                ),
            ],
            options={
                "verbose_name": "список анкоров",
                "verbose_name_plural": "списки анкоров",
                "db_table": "anchor_lists",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("product", "country"),
                        name="anchor_lists_product_id_country_key",
                        violation_error_message="Список анкоров этого региона у продукта уже есть.",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="keyword",
            name="anchor_list",
            field=models.ForeignKey(
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="keywords",
                to="keywords.anchorlist",
                verbose_name="список анкоров",
            ),
        ),
        migrations.RunPython(fill_lists, clear_lists),
    ]
