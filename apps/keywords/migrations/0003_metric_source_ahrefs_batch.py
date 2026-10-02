"""Новое значение `metric_source` — `ahrefs_batch` (ADR-045).

Само значение добавляет `sites.0013`: тип общий. Здесь — только список
вариантов поля позиции в состоянии Django, база не меняется.
"""

from django.db import migrations

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("keywords", "0002_views"),
        ("sites", "0013_ahrefs_batch_enums"),
    ]

    operations = [
        migrations.AlterField(
            model_name="keywordposition",
            name="source",
            field=config.db.PgEnumField(
                choices=[
                    ("ahrefs_api", "Ahrefs API"),
                    ("serp_api", "SERP API"),
                    ("manual", "Вручную"),
                    ("csv_import", "Импорт из файла"),
                    ("collaborator_api", "Collaborator API"),
                    ("ahrefs_batch", "Ahrefs Batch Analysis"),
                ],
                db_default="ahrefs_api",
                default="ahrefs_api",
                enum_type="metric_source",
                verbose_name="источник",
            ),
        ),
    ]
