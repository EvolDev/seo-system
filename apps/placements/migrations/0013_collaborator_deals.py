# Снимок сделок Collaborator (E11-01, ADR-079): одна строка на сделку, пишет
# только «Обновить из Collaborator». Сверка с размещениями — по снимку, без
# нового запроса к API; размещение сделки не хранится (ADR-080).

from django.db import migrations, models

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("placements", "0012_anchor_list"),
    ]

    operations = [
        migrations.CreateModel(
            name="CollaboratorDeal",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("deal_id", models.BigIntegerField(unique=True, verbose_name="сделка")),
                (
                    "project_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="проект Collaborator"
                    ),
                ),
                ("status", models.TextField(verbose_name="статус на Collaborator")),
                ("data", models.JSONField(verbose_name="сделка из API")),
                (
                    "kept",
                    models.JSONField(
                        db_default=models.Value({}, output_field=models.JSONField()),
                        default=dict,
                        verbose_name="оставлено как в системе",
                    ),
                ),
                ("fetched_at", models.DateTimeField(verbose_name="забрали")),
                (
                    "created_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="впервые"),
                ),
            ],
            options={
                "verbose_name": "сделка Collaborator",
                "verbose_name_plural": "сделки Collaborator",
                "db_table": "collaborator_deals",
            },
        ),
    ]
