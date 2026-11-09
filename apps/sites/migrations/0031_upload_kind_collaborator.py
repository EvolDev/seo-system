# Новый вид загрузки «Сверка с Collaborator» (E11-01, ADR-080): не файл, а
# действия экрана сверки — «Заполнить отмеченные» и «Взять из Collaborator»
# пишутся с журналом загрузки и отменяются, как загрузка.
#
# Ограничения `uploads` не меняются: у записи сверки есть и продавец
# (Collaborator), и продукт — вид входит в UPLOADS_FOR_PRODUCT. Значение в
# перечислении не убирается при откате — Postgres этого не умеет без
# пересоздания типа; поэтому IF NOT EXISTS (как в 0013 и 0017).

from django.db import migrations

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0030_gray_term_sets"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TYPE upload_kind ADD VALUE IF NOT EXISTS 'collaborator'",
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
                    ("anchors", "Анкоры продукта"),
                    ("collaborator", "Сверка с Collaborator"),
                ],
                enum_type="upload_kind",
                verbose_name="что загружаем",
            ),
        ),
    ]
