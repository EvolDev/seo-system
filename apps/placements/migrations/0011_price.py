# Предложение продавца у размещения (ADR-073): по какой цене и через кого его
# взяли. Колонка пустая у всех прежних строк — их план по-прежнему считается по
# рабочей цене площадки; данные миграция не трогает.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("placements", "0010_copywriter"),
        ("sites", "0029_upload_extra_prices"),
    ]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="price",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="sites.siteprice",
                verbose_name="предложение продавца",
            ),
        ),
    ]
