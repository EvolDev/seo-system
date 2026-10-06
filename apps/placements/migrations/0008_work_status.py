# Статус размещения — тот же тип work_status, что у площадки (ADR-062).
#
# Колонки меняет миграция sites.0022 одной транзакцией со своими: тип в базе
# общий, и разводить их по двум миграциям нельзя. Здесь — только состояние
# моделей Django, чтобы `makemigrations` не предлагал менять то же самое ещё раз.

from django.db import migrations

import config.db

STATUSES = [
    ("new", "Новая"),
    ("viewed", "Просмотрено"),
    ("in_work", "В работе"),
    ("ordered", "Заявка отправлена"),
    ("writing", "Написание статьи"),
    ("placed", "Размещено"),
    ("discarded", "Отбрасываю"),
    ("rejected", "Отказ"),
    ("blacklisted", "Чёрный список"),
]


class Migration(migrations.Migration):
    dependencies = [("placements", "0007_invoices"), ("sites", "0022_work_status")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="placement",
                    name="status",
                    field=config.db.PgEnumField(
                        choices=STATUSES,
                        db_default="in_work",
                        default="in_work",
                        enum_type="work_status",
                        verbose_name="статус",
                    ),
                ),
                migrations.AlterField(
                    model_name="placementstatuschange",
                    name="from_status",
                    field=config.db.PgEnumField(
                        blank=True,
                        choices=STATUSES,
                        enum_type="work_status",
                        null=True,
                        verbose_name="был",
                    ),
                ),
                migrations.AlterField(
                    model_name="placementstatuschange",
                    name="to_status",
                    field=config.db.PgEnumField(
                        choices=STATUSES, enum_type="work_status", verbose_name="стал"
                    ),
                ),
            ],
        ),
    ]
