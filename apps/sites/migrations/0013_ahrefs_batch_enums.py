"""Новые значения перечислений для выгрузок Ahrefs Batch Analysis (ADR-045).

`metric_source` — `ahrefs_batch`: замер из выгрузки Ahrefs отличается в истории
от импорта таблицы и не попадает с ним в один снимок дня. `upload_kind` —
`ahrefs_batch`: третий тип файла на экране «Загрузки». Отдельной миграцией:
новое значение перечисления нельзя использовать в той же транзакции, где его
добавили, а следующая миграция на него ссылается. Обратно значения не
убираются — Postgres этого не умеет без пересоздания типа, лишнее значение
ничему не мешает; поэтому IF NOT EXISTS — повторный накат после отката.
"""

from django.db import migrations

import config.db

SOURCES = [
    ("ahrefs_api", "Ahrefs API"),
    ("serp_api", "SERP API"),
    ("manual", "Вручную"),
    ("csv_import", "Импорт из файла"),
    ("collaborator_api", "Collaborator API"),
    ("ahrefs_batch", "Ahrefs Batch Analysis"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0012_site_latest_notes"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TYPE metric_source ADD VALUE IF NOT EXISTS 'ahrefs_batch'",
                "ALTER TYPE upload_kind ADD VALUE IF NOT EXISTS 'ahrefs_batch'",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="grayscan",
            name="method",
            field=config.db.PgEnumField(
                choices=SOURCES,
                db_default="serp_api",
                default="serp_api",
                enum_type="metric_source",
                verbose_name="способ",
            ),
        ),
        migrations.AlterField(
            model_name="sitemetric",
            name="source",
            field=config.db.PgEnumField(
                choices=SOURCES,
                db_default="manual",
                default="manual",
                enum_type="metric_source",
                verbose_name="источник",
            ),
        ),
        migrations.AlterField(
            model_name="siteprice",
            name="source",
            field=config.db.PgEnumField(
                choices=SOURCES,
                db_default="manual",
                default="manual",
                enum_type="metric_source",
                verbose_name="источник",
            ),
        ),
        migrations.AlterField(
            model_name="upload",
            name="kind",
            field=config.db.PgEnumField(
                choices=[
                    ("price_list", "Прайс продавца"),
                    ("collaborator_catalog", "Каталог Collaborator"),
                    ("ahrefs_batch", "Ahrefs Batch Analysis"),
                ],
                enum_type="upload_kind",
                verbose_name="что загружаем",
            ),
        ),
    ]
