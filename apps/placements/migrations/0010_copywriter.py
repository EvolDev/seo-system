# Копирайтер у размещения (ADR-072): кто пишет статью, цена статьи в валюте
# и ссылка на черновик. Цена своя у размещения — ставка копирайтера только
# подставляется при назначении.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0009_copywriters"),
        ("placements", "0009_link_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="copywriter",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="placements",
                to="content.copywriter",
                verbose_name="копирайтер",
            ),
        ),
        migrations.AddField(
            model_name="placement",
            name="copywriter_cents",
            field=models.IntegerField(
                blank=True, null=True, verbose_name="цена копирайтера, центы"
            ),
        ),
        migrations.AddField(
            model_name="placement",
            name="copywriter_currency",
            field=models.CharField(
                blank=True, max_length=3, null=True, verbose_name="валюта копирайтера"
            ),
        ),
        migrations.AddField(
            model_name="placement",
            name="draft_url",
            field=models.TextField(blank=True, null=True, verbose_name="черновик"),
        ),
    ]
