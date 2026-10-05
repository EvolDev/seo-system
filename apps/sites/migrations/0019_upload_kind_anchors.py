"""Новый тип загрузки: анкоры продукта (E3-05, ADR-059).

Отдельной миграцией, как `0017`: новое значение перечисления нельзя
использовать в той же транзакции, где его добавили, а следующая миграция
ссылается на него в CHECK. Обратно значение не убирается.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0018_uploads_placements_refdomains"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TYPE upload_kind ADD VALUE IF NOT EXISTS 'anchors'",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
