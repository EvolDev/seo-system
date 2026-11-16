"""Индексы на ссылки, которые Postgres проверяет при удалении площадки и цены.

16.11.2026 отмена загрузки №104 (3410 строк) не дождалась подтверждения: gunicorn
снял запрос через 180 с. Внешние ключи отложенные — на `SET CONSTRAINTS ALL
IMMEDIATE` (`config.deletion`) база проверяет каждую удалённую цену и площадку:
не ссылается ли на неё ещё строка разбора или площадка. Без индекса на колонке
ссылки каждая проверка — полный проход по таблице: ~11 тыс. проходов по
`upload_items` (147 тыс. строк) и `sites` по 12–18 мс. С индексами — поиск по нему.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0032_upload_changes_after"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="site",
            index=models.Index(fields=["price"], name="idx_sites_price"),
        ),
        migrations.AddIndex(
            model_name="uploaditem",
            index=models.Index(fields=["site"], name="idx_upload_items_site"),
        ),
        migrations.AddIndex(
            model_name="uploaditem",
            index=models.Index(fields=["price"], name="idx_upload_items_price"),
        ),
        migrations.AddIndex(
            model_name="uploaditem",
            index=models.Index(fields=["ref_price"], name="idx_upload_items_ref_price"),
        ),
    ]
