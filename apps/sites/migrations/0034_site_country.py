"""Страна площадки — из каталога продавца или выбором в карточке (ADR-082).

Пользователь (16.11.2026): каталог продавца `sites.xlsx` несёт колонку `country`
(Brazil — 1450 площадок, Portugal — 133…), а в «Площадках» нужен фильтр, чтобы
брать только бразильские и португальские. Регион Ahrefs (ADR-045) — замер трафика
по странам, а не то, о чём площадка; поэтому — своя колонка. Пусто — не знаем.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0033_fk_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="country",
            field=models.CharField(
                blank=True, max_length=2, null=True, verbose_name="страна площадки"
            ),
        ),
    ]
