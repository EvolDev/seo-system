"""Новые типы загрузки: размещения продукта и ссылающиеся домены Ahrefs (E1-09, ADR-051).

Отдельной миграцией, как `0013`: новое значение перечисления нельзя
использовать в той же транзакции, где его добавили, а следующая миграция
ссылается на него в CHECK. Обратно значения не убираются — Postgres этого не
умеет без пересоздания типа; поэтому IF NOT EXISTS — повторный накат после
отката.
"""

from django.db import migrations

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0016_status_changes"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TYPE upload_kind ADD VALUE IF NOT EXISTS 'placements'",
                "ALTER TYPE upload_kind ADD VALUE IF NOT EXISTS 'ahrefs_refdomains'",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="upload",
            name="kind",
            field=config.db.PgEnumField(
                choices=[
                    ("price_list", "Прайс продавца"),
                    ("collaborator_catalog", "Каталог Collaborator"),
                    ("ahrefs_batch", "Ahrefs Batch Analysis"),
                    ("placements", "Размещения продукта"),
                    ("ahrefs_refdomains", "Ссылающиеся домены Ahrefs"),
                ],
                enum_type="upload_kind",
                verbose_name="что загружаем",
            ),
        ),
    ]
