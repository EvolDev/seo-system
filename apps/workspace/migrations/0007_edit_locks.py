# Блокировка правки площадки и размещений (ADR-077).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("workspace", "0006_region_columns"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EditLock",
            fields=[
                (
                    "key",
                    models.TextField(primary_key=True, serialize=False, verbose_name="что правят"),
                ),
                (
                    "started_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="правит с"),
                ),
                (
                    "seen_at",
                    models.DateTimeField(
                        db_default=config.db.PgNow(), verbose_name="последняя правка"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="кто правит",
                    ),
                ),
            ],
            options={
                "verbose_name": "блокировка правки",
                "verbose_name_plural": "блокировки правки",
                "db_table": "edit_locks",
            },
        ),
    ]
