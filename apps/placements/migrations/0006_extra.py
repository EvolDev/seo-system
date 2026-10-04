"""Прочие данные строки файла у размещения (E1-09, ADR-051, schema.sql 1.13).

Загрузка размещений кладёт сюда колонки файла без своего поля: заголовок →
значение. Из файла ничего не теряется (ADR-041).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("placements", "0005_status_changes"),
    ]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="extra",
            field=models.JSONField(blank=True, null=True, verbose_name="прочие данные из файла"),
        ),
    ]
