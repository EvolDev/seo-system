# Написание и анонс одной суммой на все площадки прайса (ADR-071): продавцы
# обычно называют их отдельно от файла. Сумма — у загрузки, в валюте файла;
# площадке достаётся, только если в файле своей цены нет.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0028_offer_withdrawn"),
    ]

    operations = [
        migrations.AddField(
            model_name="upload",
            name="announce_cents",
            field=models.IntegerField(blank=True, null=True, verbose_name="анонс для всех, центы"),
        ),
        migrations.AddField(
            model_name="upload",
            name="writing_cents",
            field=models.IntegerField(
                blank=True, null=True, verbose_name="написание для всех, центы"
            ),
        ),
    ]
